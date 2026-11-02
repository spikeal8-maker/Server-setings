# Повторяемая HTTP-проверка 20 → 50 → 100 учеников

Состояние: подготовлен исходный инструмент и локальные контрактные тесты. Живая школа этим изменением не нагружалась. Это не доказательство готовности выпуска: `releaseAccepted` всегда `false`. Исторические FAIL первого запуска Scratch, burst429 и штатного восстановления из документа36 остаются историческими FAIL, пока отдельно не проверены исправления.

## Область проверки

`scripts/asa-release-load.py` поддерживает 20/50/100 реальных различных синтетических seat-сессий. `core` создаёт личные проекты каждого ученика в three-d, electronics и blocks; открывает, изменяет, сохраняет, повторно читает документ, PNG snapshot и Scratch SVG asset. Новый Scratch asset читается после commit содержащего его draft. Для каждого проекта следующий ученик получает ожидаемый403/404; эти проверки считаются отдельно от ошибок. У первого ученика каждого модуля также проверяются duplicate/rename/archive/restore/trash/restore. Максимум новых QA-проектов: 3N+3, то есть303 при100 учениках. Постоянное удаление не выполняется; оператор управляет сроком хранения QA-данных отдельно.

`mixed` добавляет детерминированную смесь чтения пяти групп: модули/игры, обучение/задания/результаты, база знаний/опубликованный курс, коллекции/публичные проекты, AI status. Перед волной выполняются отдельные чтения Teacher/Admin и отрицательные проверки доступа к admin. Fixture содержит отдельного синтетического преподавателя с educator и content_author capabilities и отдельного краткоживущего platform-admin. Их права не выдаются существующим пользователям этим инструментом.

Полная матрица маршрутов, ролей, семейств и API-проверок находится в [school-route-inventory.json](../profiles/school-route-inventory.json); настройки — в [school-release-load-settings.json](../profiles/school-release-load-settings.json). Здесь64 route entries: включая aliases, standalone/hash editors, вложенные шахматные и admin страницы. HTML200 не считается работающей страницей. Маршрут получает только `HTTP_DATA_CONTRACT_VERIFIED`, если проверены его обязательные API; login/signup forms, help и прочие браузерные действия остаются NOT_RUN.

Матрица закрепляет канонические Git blob SHA256 исходников ASA. Mixed apply требует чистый checkout ровно той ревизии, которую сообщает runtime. Любое изменение закреплённого файла останавливает проверку до сетевой нагрузки. Перед новым кандидатом матрицу должен актуализировать автор и проверить независимый reviewer; обход проверки или переписывание хешей без просмотра diff не является актуализацией покрытия.

### Проверка исходников кандидата 0489c430 — 02.11.2026

Декларации сверены с точным Git tree
`0489c430d1e9d06631c970c29e30ea2229c2c9f0`, после контролируемой
конвергенции приложения с canonical `60264f69`. Это проверка исходников,
не установленной версии и не нагрузки. Контракт fixture сохраняет 11 файлов,
маршрутов — 31, mixed resources — 20. Из 62 закреплённых ссылок изменились
только две; остальные 60 SHA256 совпали. Все 64 записи маршрутов, роли и
положительные проверки сохранены без изменений.

| Пересмотренный файл | Изменение формы API |
|---|---|
| `apps/web/src/api.ts` | `openProject` принимает необязательный AbortSignal и передаёт его прежнему GET `/api/projects/{id}`; URL, авторизация и форма результата сохранены |
| `packages/contracts/src/public-project-display-artifact.ts` | Добавлен типизированный `PublicThreeDArtifact` schemaVersion4 и его закрытый валидатор; прежние форматы Electronics/Scratch не заменены |

Просмотрены причинные изменения с `0d693050`: серверная проекция
`public-project-artifact.ts`, новый `public-three-d-artifact.ts`,
`PublicThreeDViewer`, `ThreeDPresentation`, преобразование публичного документа и
hover preview. Публичный контроллер не изменился: используется прежний GET
`/api/public-projects/{publicationId}/artifact?revisionId={revisionId}`.
Новый маршрут не добавлен. Обязательные envelope/provenance/readOnly поля
сохранены и удовлетворяют прежним проверкам runner и mixed resources.

Артефакт 3D имеет `status=display`, `viewerFamily=three-d`, единицы mm,
`readOnly=true`, `interactive=true`, `download=false`; разрешены только закрытые
поля геометрии, до 256 узлов, 512 KiB и 2048 sketch points. Проекция читает
текущую разрешённую неизменную публикацию через прежний STABLE SQL read,
снова проверяя доступ и при попадании в кэш. Добавленный adapter не содержит
save/copy/export/publication/AI-команд. Клиентский renderer получает документ с
заблокированными узлами; callbacks выбора и изменения не сохраняют проект.

Read-only не означает отсутствие затрат: hover может добавить GET, сервер —
логирование, вычисление проекции и кэш, браузер — geometry/WebGL CPU/GPU и память.
Для private preview используется прежний авторизованный GET проекта, а не
обход проверки владельца. Есть отмена запроса, освобождение renderer при выходе,
скрытии/размонтировании и fallback изображения. Их реальная надёжность и цена
при 100 учениках требуют отдельной браузерной проверки. HTTP-контракт не
подтверждает готовность WebGL, кадровую частоту или качество изображения.

SHA256 заменены только для двух перечисленных файлов после просмотра diff;
provenance всех трёх деклараций указывает наблюдавшийся tree. Финальный
runtime может иметь более поздний commit с теми же исходниками, однако его
revision/clean checkout/build и все закреплённые blobs должны заново пройти
прежние проверки. Это не разрешение автоматически принимать следующий drift.
SSE, local-only AI, запрет платного fallback, владение QA и цепочка20/50/100
не изменены. Офлайн-набор: 56 тестов OK с одним Windows skip. Планы20/50/100
вернули ноль сетевых запросов и чтений fixture. Настоящие source readers
маршрутов и mixed resources прошли против чистого `2259ef92`: после0489 в нём
изменены только два одобренных e2e-теста, runtime blobs остались теми же.
Это подтверждает сохранение revision/clean-checkout guards без дополнительной
перепривязки хешей. Несекретная квитанция:
[`school-source-inventory-alignment.json`](../snapshots/2026-11-02/school-source-inventory-alignment.json).

## Запуск и ограничение воздействия

Без `--apply` выполняется только plan: нет чтения fixture/source и нет сети. Пример:

```sh
python3 scripts/asa-release-load.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 287 --users 20 --profile core
```

Фактический запуск выполняет оператор на утверждённом хосте с приватным JSON fixture (0600, regular file, без symlink/hardlink), новым путём отчёта и явным `--apply`. Поля: `authorizedSyntheticFixture:true`, точный HTTPS `origin`, `seats:[{seat:UUID,token:SESSION}]`. Для mixed нужны `roles.teacher/admin:{accountId:UUID,token:SESSION}`, `resources:{classroomId,classProjectId,publicationId,publicCourseId,courseRunId,lessonId}` и `expectedRequiredLogSourceIds` — полный операторский набор обязательных подключённых источников. Документы ресурсов должны принадлежать этой QA-когорте. Реальные токены нельзя помещать в Git, CI, командную строку или примеры отчётов. Windows permission bits не доказывают NTFS ACL: live fixture следует использовать на Linux с0600 или отдельно проверенной приватной ACL.

```sh
python3 scripts/asa-release-load.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 287 --users 20 --profile mixed --source-root /private/asa-source --fixture /private/qa-fixture.json --output /private/mixed20.json --apply
```

Далее50 требует `--previous-receipt /private/mixed20.json`, а100 — успешный50. Формат v3 связывает отчёт с каноническим HTTPS origin, ревизией/schema, SHA256 всей QA-когорты (seat/account IDs, resources и документы, без токенов), source inventory и точными параметрами нагрузки. На всех ступенях используют одинаковый полный fixture; токены можно обновлять без смены IDs. Profile/pace/latency/AI counts/AI budgets/deadline должны совпадать:50 с одним изображением не разрешает100 с десятью.

Предыдущая ступень должна завершиться не более30 минут назад; запрещены future timestamps, незавершённый stage, ошибки, непустые inFlight, отсутствующие runtime provenance, telemetry, критические requests или journeys. Проверяются все N×3/N×4 пары ученик/модуль, счётчики модулей, distinct identities, isolation polarity, пересчитанные latency/error summaries и mixed source/API evidence. Цепочка20→50→100 хранит SHA256 предыдущих receipts с тем же deployment/workload binding и временем. Это проверка полноты и согласованности операторских evidence, а не криптографическая аттестация или защита от намеренной подделки владельцем хоста. Старые v1/v2 receipts не принимаются. Core и mixed имеют самостоятельные лестницы. Не менять pace между ступенями; paced PASS не заменяет burst acceptance.

Стандартные пределы: p95≤5000ms и p99≤10000ms по каждой HTTP-операции, wall deadline600s (допустимо30..900), think time0ms. Новая неожиданная ошибка прекращает новые запросы с credentials. TLS проверяется; origin точный; redirects и автоматические retries запрещены. Начальный отчёт всегда running/FAIL, partial receipt обновляется атомарно. Родительский процесс ограничивает зависшего ребёнка; известные own request/project IDs сохраняются для разбирательства. После жёсткого завершения отмена остаётся BEST_EFFORT_NOT_VERIFIED, поэтому нельзя сразу повторять нагрузку, не проверив собственные задания.

## Наблюдаемость и AI

До/после сверяются runtime revision и synchronized schema. В production школы публичный и локальный `/health/metrics` возвращают404 (операторская проверка контроллера); это штатная закрытая поверхность, не повод включать `ASA_PUBLIC_METRICS`. Mixed и любой fixture с `roles.admin` используют существующий защищённый GET `/api/admin/v1/operations/status` и только числовой allowlist его `.runtime` (RSS, event-loop, HTTP, DB wait и прочее). Используется отдельная QA admin cookie после прежних проверок account identity и platform_admin в `/api/admin/v1/me`. Контроллер требует admin session, сервис — `administration.operations.read` на platform scope; тот же RuntimeMetrics подключён через app.module. Полный административный ответ, counts, PII и credentials не сохраняются.

Частная выборка требует точных revision/schema, synchronized=true, responding API/DB, object runtime и checkedAt возрастом0..30 секунд. 401/403/404, null, неверная форма и отсутствие обязательных числовых полей вызывают FAIL без публичного fallback. Legacy core без admin сохраняет прежний публичный путь: он пригоден только там, где endpoint уже явно доступен;404 также FAIL. Источник выбирается до I/O, записывается в `deploymentBinding.telemetrySource` и отчёт, связан с cohort digest и не меняется между20→50→100. Числовой sampledAt должен лежать внутри времени прогона; проверяется actor каждого runtime-metrics request. Старые v3 receipts без этого provenance не разрешают повышение нагрузки.

К существующим31 source pins добавлен только просмотренный `apps/api/src/app.module.ts` (SHA256 `de1ca10f39133c3fdf90fba29289f1d2b5073e27234d13ac0cee6d69bf56d759`); прежние31 и все64 routes/roles сохранены. Реальный read-only reader32 pins прошёл на чистом application checkout `a69d7f8ce23e3550ed1fa6b49a8a8cb62f675b1b`. Это source-only подтверждение пути, не живая проверка нагрузки или выдачи прав.

Проверка журналов требует `requiredCoverage` version1/policy `asa-connected-v1`, state ok, native ok, historyAudit ok, точного непустого `requiredSourceIds` и свежих checkedAt/collectedThrough по каждому обязательному источнику. Summaries явно имеют requireThrough=false. Глобальное `coverageComplete:false` допустимо и не означает потерю этих ограниченных подключённых источников; это честная граница полного исторического архива.

AI выключен по умолчанию. В mixed оператор отдельно задаёт `--ai-text-users N --ai-images K` (K≤10). Активный provider каждого выбранного ученика обязан быть ollama с model ollama-local; доступность local в списке при платном default недостаточна. Запрос явно local, без web search/native image; cloud/paid fallback запрещён. Изображения требуют ready local ComfyUI и разрешённого локального recipe768.

Текст проверяет SSE comments/first byte/first token/один done/сохранённую историю; результат сохраняет только длину и SHA256 текста. Для image проверяются admission, ограниченный polling, собственные thread/request IDs, сохранённые PNG bytes/CRC/dimensions и повторное чтение. Повтор того же подтверждённого requestId — отдельная idempotency-проверка. Последнее image задание отменяется; только после подтверждённого cancelled отправляется один новый UUID retry. Если job успел завершиться, проверка отмены FAIL. При K изображениях максимум K+1 новых jobs. Доказательство отсутствия повторного provider dispatch и admission100 принадлежит отдельным SQL/queue CI, а не HTTP replay.

Ограничения AI по умолчанию: first byte5000ms, first token120000ms, complete240000ms; локальный stream дополнительно ограничен180s. В отчёте отдельно AI latency/error/state. Эти пределы — критерий приёмки, а не обещание производительности.

## Отдельные обязательные доказательства

100 браузеров, первый запуск Scratch, WebGL/3D rendering, численная симуляция цепей, действия login/admin forms, реальное обновление/rollback/restore, source-IP/NAT из класса и100-job SQL queue correctness остаются NOT_RUN. Fixture может содержать `evidenceReferences` только как `sha256:<digest>`; ссылка сама не превращает NOT_RUN в PASS. Не установлен publisher/update hook. Полная приёмка требует отдельных проверенных evidence и решения владельца.

CI workflow `release-load-contract.yml` проверяет только синтетические transport fixtures и plan20/50/100, без школьных адресов/credentials/нагрузки. Локально: `python -m unittest discover -s tests -p 'test_release_load*.py' -v`. Проверены отказы fixture, redirects, неверная identity/schema, HTTP429, HTML вместо API, paid default, stale/missing logs, route drift, hard deadline/partial receipt, SSE framing, scoped cancel/retry и PNG corruption. Snapshot фиксирует именно author-prepared/NOT_RUN, не реальный100-user PASS.
