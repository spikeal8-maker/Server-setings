# Повторяемая HTTP-проверка 20 → 50 → 100 учеников

Состояние: подготовлен исходный инструмент и локальные контрактные тесты. Живая школа этим изменением не нагружалась. Это не доказательство готовности выпуска: `releaseAccepted` всегда `false`. Исторические FAIL первого запуска Scratch, burst429 и штатного восстановления из документа36 остаются историческими FAIL, пока отдельно не проверены исправления.

## Область проверки

`scripts/asa-release-load.py` поддерживает 20/50/100 реальных различных синтетических seat-сессий. `core` создаёт личные проекты каждого ученика в three-d, electronics и blocks; открывает, изменяет, сохраняет, повторно читает документ, PNG snapshot и Scratch SVG asset. Новый Scratch asset читается после commit содержащего его draft. Для каждого проекта следующий ученик получает ожидаемый403/404; эти проверки считаются отдельно от ошибок. У первого ученика каждого модуля также проверяются duplicate/rename/archive/restore/trash/restore. Максимум новых QA-проектов: 3N+3, то есть303 при100 учениках. Постоянное удаление не выполняется; оператор управляет сроком хранения QA-данных отдельно.

`mixed` добавляет детерминированную смесь чтения пяти групп: модули/игры, обучение/задания/результаты, база знаний/опубликованный курс, коллекции/публичные проекты, AI status. Перед волной выполняются отдельные чтения Teacher/Admin и отрицательные проверки доступа к admin. Fixture содержит отдельного синтетического преподавателя с educator и content_author capabilities и отдельного краткоживущего platform-admin. Их права не выдаются существующим пользователям этим инструментом.

Полная матрица маршрутов, ролей, семейств и API-проверок находится в [school-route-inventory.json](../profiles/school-route-inventory.json); настройки — в [school-release-load-settings.json](../profiles/school-release-load-settings.json). Здесь 65 route entries: включая aliases, standalone/hash editors, вложенные шахматные и admin страницы. HTML200 не считается работающей страницей. Маршрут получает только `HTTP_DATA_CONTRACT_VERIFIED`, если проверены его обязательные API; login/signup forms, help и прочие браузерные действия остаются NOT_RUN.

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

### Сведение с canonical и контракт кандидата 53a83e53 — 02.11.2026

Текущие декларации сверены с чистым Git checkout приложения
`53a83e53d2ef40901a698082b215a314603e8d97` (programme `854b6b97` + canonical
`c3399eb4`). Инфраструктура включает frozen main
`05ea148c007cf2c2b56729294599c2d611a3c14a`: добавления 1Panel/SSH сохранены,
обе части конфликта таблицы проверок сохранены полностью. Это сведение исходников,
без применения настроек, обновления сервера или изменения рабочих данных.

Все 11 fixture pins и 23 mixed-resource pins совпали без изменения хешей.
Старые 32 route pins сохранены; из них после просмотра причинного diff изменены
семь: navigation, AI chat/image controllers, chat/image services, Ollama adapter и
app.module. Сохранены SSE heartbeat, worker diagnostics, `queuePaused`, required
log coverage и защищённая runtime telemetry. Ещё 12 pins охватывают сетевые
шахматы, working files и SQL 0294/0296/0298/0300; всего 44 route pins.

Изменения API и границы проверки:

| Изменение приложения | Проверка и ограничение |
|---|---|
| Новая `/#/chess/:projectId/network`, роли Seat/Teacher/Admin | Два обычных GET: `/api/games/chess/access` и `/history?limit=10`, с фактической сессией QA ученика. Проверяются UUID игрока, закрытые policy scopes/reason, пустая собственная история. Teacher deny наблюдается без изменения policy. Приглашения, ходы, сетевой матч и браузер остаются NOT_RUN |
| Частные working-files list/read/restore | Каждый собственный только что созданный AI thread проходит GET списка и проверку `items=[]` до обращения к модели. Чужие/заполненные или некорректные ответы и HTTP 429 останавливают работу. Файлы не создаются, не читаются по имени и не восстанавливаются; эти действия NOT_RUN |
| Native image file binding 0298 | Отдельный wrapper и trigger добавляют запись в точный HTML placeholder. Прежние прямые image POST/get/cancel/PNG-download не изменены; тест использует их. Реальная вставка картинки в HTML NOT_RUN |
| Polza, document/file tools, новые activity events | Explicit `model=ollama-local` по-прежнему выбирает локальный adapter; прежний fail-closed preflight и запрет платного fallback сохранены. Документные вложения, file tools и платные провайдеры этим workload не проверяются |
| Канонические SQL 0294–0299 и fair queue 0300 | Canonical history сохранена, очередь перенесена в 0300. Максимальная схема исходников — 300; прежняя репетиция 286→294 не подтверждает обновление 286→300 |

Все исходные 64 route entries с ролями, checks и ограничениями сохранены побайтно
на уровне нормализованного JSON (отдельная regression digest). Новая запись
требует оба network GET; каждая браузерная запись по-прежнему NOT_RUN. Пять групп
mixed wave сохранены, к группе модулей/игр добавлены два network GET. AI receipt
для перехода 20→50→100 требует также `emptyWorkingFilesVerified=true`.

Проверка реальными source readers: PASS для 11/44/23 pins и 65 routes на точном
чистом checkout. Офлайн workload suite: 85 tests, 84 PASS и один POSIX skip на
Windows; 11 тестов добавлений canonical 1Panel/ACL также PASS. Полный Linux suite
должен пройти CI на точном infra SHA: локальный Windows запуск не принят из-за
POSIX-модулей, permissions/symlink/procfd, разделителей путей и locale decoding.
Фактическая нагрузка, обновление 286→300 и fresh-host приёмка остаются NOT_RUN.
Доказательства: [snapshot](../snapshots/2026-11-02/school-canonical-convergence-source.json).

## Запуск и ограничение воздействия

Без `--apply` выполняется только plan: нет чтения fixture/source и нет сети. Пример:

```sh
python3 scripts/asa-release-load.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 300 --users 20 --profile core
```

Фактический запуск выполняет оператор на утверждённом хосте с приватным JSON fixture (0600, regular file, без symlink/hardlink), новым путём отчёта и явным `--apply`. Поля: `authorizedSyntheticFixture:true`, точный HTTPS `origin`, `seats:[{seat:UUID,token:SESSION}]`. Для mixed нужны `roles.teacher/admin:{accountId:UUID,token:SESSION}`, `resources:{classroomId,classProjectId,publicationId,publicCourseId,courseRunId,lessonId}` и `expectedRequiredLogSourceIds` — полный операторский набор обязательных подключённых источников. Документы ресурсов должны принадлежать этой QA-когорте. Реальные токены нельзя помещать в Git, CI, командную строку или примеры отчётов. Windows permission bits не доказывают NTFS ACL: live fixture следует использовать на Linux с0600 или отдельно проверенной приватной ACL.

```sh
python3 scripts/asa-release-load.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 300 --users 20 --profile mixed --source-root /private/asa-source --fixture /private/qa-fixture.json --output /private/mixed20.json --apply
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

## Дополнительные браузерные ступени 15 → 30 → 50 → 75 → 100

Сценарий: `scripts/school-browser-load.mjs` запускается на **внешнем генераторе** (D2-R2 / домашняя Linux VM), но не на школьной VM100.
Node.js 22+, локальный Chrome и отдельно установленный Puppeteer 25.16.0. По умолчанию — PLAN-ONLY, без сети или браузера.
Реальный запуск требует `--apply`, точного опубликованного `--revision`, отдельного `--output`, `--browser` с абсолютным путём к Chrome.
Пример только плана для действующего релиза (после обновления проекта SHA надо поменять):

    node scripts/school-browser-load.mjs --origin https://x.asa-lab.ru --revision 3e8713476cafedda3e24a39665458e833efd7c22 --stage 15 --seconds 45

Отдельные stages: 15, 30, 50, 75 и 100. Переходить только после PASS предыдущей ступени и проверки CPU/RAM/журналов/контейнеров.
Тест использует независимые анонимные Chrome-контексты: главная, страницы Electronics/3D/Blocks, оболочка входа в проекты и прокрутка.
**НЕ проверяет** настоящий авторизованный Scratch/3D/WebGL/электронику, создание/сохранение проектов, БД и MinIO.
Для этих действий отдельно использовать существующий авторизованный ASA HTTP load с owned QA-учениками, а затем настоящий браузерный e2e с отдельной QA-когортой.
Браузерный stage блокируется при HTTP429/5xx, несоответствии SHA, повторном отказе readiness, p95 более 5с и p99 более 10с.
Источник D2-R2 (одна сеть) не заменяет второго независимого источника H-03 и школьный вход 30 учеников за общим NAT.
Полное принятие перед переносом asa-lab.ru также требует актуального age/restore, 1Panel/FRP, подтверждённого покрытия логов и долгого soak-теста.

### Проверка 09.11.2026 — диагностический лимит за одним IP

На D2-R2: 15 Chrome PASS (569 переходов/46,8с; 0 HTTP429/5xx; p95=220мс).
При 30 Chrome с **нереалистичной** паузой ~0,75–1,4с получено 6 HTTP429/55,3с, при этом 0 HTTP5xx, 0 JS ошибок.
Серверные коррелирующие JSON-логи web+API идентифицируют эти 429 как `POST /api/diagnostics/quality`, а не сохранение проектов.
В ASA Lab этот анонимный endpoint использует `FixedWindowRateLimiter(600/60с)` по клиентскому IP;
учебный NAT может объединять многих учеников. Не увеличивать лимит вслепую и не отключать защиту.
Контрольный умеренный HTTP-тест с H-03: 30 параллельных клиентов, 62с, 189/189 HTTP200, 0 ошибок, p95=134мс.
Новая версия браузерного генератора поддерживает `--think-ms 4000..20000`, по умолчанию `12000` (имитация просмотра страницы).
Генератор отдельно считает `diagnosticQuality429` и `other429`; оба остаются красным сигналом для приёмки.
Это **не является** PASS настоящих авторизованных редакторов или разрешением перевести `asa-lab.ru`.
Диагностический и cutover blocker: https://github.com/spikeal8-maker/asa-lab/issues/1032.
