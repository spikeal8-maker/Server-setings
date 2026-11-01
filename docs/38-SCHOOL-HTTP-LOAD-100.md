# Повторяемая HTTP-проверка 20 → 50 → 100 учеников

Состояние: подготовлен исходный инструмент и локальные контрактные тесты. Живая школа этим изменением не нагружалась. Это не доказательство готовности выпуска: `releaseAccepted` всегда `false`. Исторические FAIL первого запуска Scratch, burst429 и штатного восстановления из документа36 остаются историческими FAIL, пока отдельно не проверены исправления.

## Область проверки

`scripts/asa-release-load.py` поддерживает 20/50/100 реальных различных синтетических seat-сессий. `core` создаёт личные проекты каждого ученика в three-d, electronics и blocks; открывает, изменяет, сохраняет, повторно читает документ, PNG snapshot и Scratch SVG asset. Новый Scratch asset читается после commit содержащего его draft. Для каждого проекта следующий ученик получает ожидаемый403/404; эти проверки считаются отдельно от ошибок. У первого ученика каждого модуля также проверяются duplicate/rename/archive/restore/trash/restore. Максимум новых QA-проектов: 3N+3, то есть303 при100 учениках. Постоянное удаление не выполняется; оператор управляет сроком хранения QA-данных отдельно.

`mixed` добавляет детерминированную смесь чтения пяти групп: модули/игры, обучение/задания/результаты, база знаний/опубликованный курс, коллекции/публичные проекты, AI status. Перед волной выполняются отдельные чтения Teacher/Admin и отрицательные проверки доступа к admin. Fixture содержит отдельного синтетического преподавателя с educator и content_author capabilities и отдельного краткоживущего platform-admin. Их права не выдаются существующим пользователям этим инструментом.

Полная матрица маршрутов, ролей, семейств и API-проверок находится в [school-route-inventory.json](../profiles/school-route-inventory.json); настройки — в [school-release-load-settings.json](../profiles/school-release-load-settings.json). Здесь64 route entries: включая aliases, standalone/hash editors, вложенные шахматные и admin страницы. HTML200 не считается работающей страницей. Маршрут получает только `HTTP_DATA_CONTRACT_VERIFIED`, если проверены его обязательные API; login/signup forms, help и прочие браузерные действия остаются NOT_RUN.

Матрица закрепляет канонические Git blob SHA256 исходников ASA. Mixed apply требует чистый checkout ровно той ревизии, которую сообщает runtime. Любое изменение закреплённого файла останавливает проверку до сетевой нагрузки. Перед новым кандидатом матрицу должен актуализировать автор и проверить независимый reviewer; обход проверки или переписывание хешей без просмотра diff не является актуализацией покрытия.

## Запуск и ограничение воздействия

Без `--apply` выполняется только plan: нет чтения fixture/source и нет сети. Пример:

```sh
python3 scripts/asa-release-load.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 287 --users 20 --profile core
```

Фактический запуск выполняет оператор на утверждённом хосте с приватным JSON fixture (0600, regular file, без symlink/hardlink), новым путём отчёта и явным `--apply`. Поля: `authorizedSyntheticFixture:true`, точный HTTPS `origin`, `seats:[{seat:UUID,token:SESSION}]`. Для mixed нужны `roles.teacher/admin:{accountId:UUID,token:SESSION}`, `resources:{classroomId,classProjectId,publicationId,publicCourseId,courseRunId,lessonId}` и `expectedRequiredLogSourceIds` — полный операторский набор обязательных подключённых источников. Документы ресурсов должны принадлежать этой QA-когорте. Реальные токены нельзя помещать в Git, CI, командную строку или примеры отчётов. Windows permission bits не доказывают NTFS ACL: live fixture следует использовать на Linux с0600 или отдельно проверенной приватной ACL.

```sh
python3 scripts/asa-release-load.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 287 --users 20 --profile mixed --source-root /private/asa-source --fixture /private/qa-fixture.json --output /private/mixed20.json --apply
```

Далее50 требует `--previous-receipt /private/mixed20.json`, а100 — успешный50. Ревизия/schema/profile/pace/latency limits должны совпадать. Это проверка согласованности операторских evidence, а не криптографическая аттестация отчёта. Старые v1 receipts не принимаются. Core и mixed имеют самостоятельные лестницы. Не менять pace между ступенями; paced PASS не заменяет burst acceptance.

Стандартные пределы: p95≤5000ms и p99≤10000ms по каждой HTTP-операции, wall deadline600s (допустимо30..900), think time0ms. Новая неожиданная ошибка прекращает новые запросы с credentials. TLS проверяется; origin точный; redirects и автоматические retries запрещены. Начальный отчёт всегда running/FAIL, partial receipt обновляется атомарно. Родительский процесс ограничивает зависшего ребёнка; известные own request/project IDs сохраняются для разбирательства. После жёсткого завершения отмена остаётся BEST_EFFORT_NOT_VERIFIED, поэтому нельзя сразу повторять нагрузку, не проверив собственные задания.

## Наблюдаемость и AI

До/после сверяются runtime revision и synchronized schema. Во время работы читается только числовой allowlist `/health/metrics` (RSS, event-loop, HTTP, DB wait и прочее); пропавшие обязательные метрики вызывают FAIL. Проверка журналов требует `requiredCoverage` version1/policy `asa-connected-v1`, state ok, native ok, historyAudit ok, точного непустого `requiredSourceIds` и свежих checkedAt/collectedThrough по каждому обязательному источнику. Summaries явно имеют requireThrough=false. Глобальное `coverageComplete:false` допустимо и не означает потерю этих ограниченных подключённых источников; это честная граница полного исторического архива.

AI выключен по умолчанию. В mixed оператор отдельно задаёт `--ai-text-users N --ai-images K` (K≤10). Активный provider каждого выбранного ученика обязан быть ollama с model ollama-local; доступность local в списке при платном default недостаточна. Запрос явно local, без web search/native image; cloud/paid fallback запрещён. Изображения требуют ready local ComfyUI и разрешённого локального recipe768.

Текст проверяет SSE comments/first byte/first token/один done/сохранённую историю; результат сохраняет только длину и SHA256 текста. Для image проверяются admission, ограниченный polling, собственные thread/request IDs, сохранённые PNG bytes/CRC/dimensions и повторное чтение. Повтор того же подтверждённого requestId — отдельная idempotency-проверка. Последнее image задание отменяется; только после подтверждённого cancelled отправляется один новый UUID retry. Если job успел завершиться, проверка отмены FAIL. При K изображениях максимум K+1 новых jobs. Доказательство отсутствия повторного provider dispatch и admission100 принадлежит отдельным SQL/queue CI, а не HTTP replay.

Ограничения AI по умолчанию: first byte5000ms, first token120000ms, complete240000ms; локальный stream дополнительно ограничен180s. В отчёте отдельно AI latency/error/state. Эти пределы — критерий приёмки, а не обещание производительности.

## Отдельные обязательные доказательства

100 браузеров, первый запуск Scratch, WebGL/3D rendering, численная симуляция цепей, действия login/admin forms, реальное обновление/rollback/restore, source-IP/NAT из класса и100-job SQL queue correctness остаются NOT_RUN. Fixture может содержать `evidenceReferences` только как `sha256:<digest>`; ссылка сама не превращает NOT_RUN в PASS. Не установлен publisher/update hook. Полная приёмка требует отдельных проверенных evidence и решения владельца.

CI workflow `release-load-contract.yml` проверяет только синтетические transport fixtures и plan20/50/100, без школьных адресов/credentials/нагрузки. Локально: `python -m unittest discover -s tests -p 'test_release_load*.py' -v`. Проверены отказы fixture, redirects, неверная identity/schema, HTTP429, HTML вместо API, paid default, stale/missing logs, route drift, hard deadline/partial receipt, SSE framing, scoped cancel/retry и PNG corruption. Snapshot фиксирует именно author-prepared/NOT_RUN, не реальный100-user PASS.
