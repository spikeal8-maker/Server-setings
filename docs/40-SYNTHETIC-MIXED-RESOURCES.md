# Собственные QA-ресурсы mixed-теста

`scripts/release_load_resources.py` дополняет reviewed core fixture из
[документа39](39-SYNTHETIC-ACCEPTANCE-FIXTURE.md) через штатные API. Сам срез
содержит только source/tests; на сервере ничего не создавалось. По умолчанию
plan не читает fixture/source и не запускает subprocess/HTTP/Docker/БД:

```sh
python3 scripts/release_load_resources.py --origin https://school.example --revision FULL_FINAL_RUNTIME_SHA --schema 294 --label QA-class-acceptance
```

Пример schema294 не заменяет фактическую версию runtime. Оператор явно передаёт
полный SHA финального опубликованного кандидата и его схему. Источник0d693050,
использованный для аудита ниже, — наблюдаемый source snapshot, не разрешение
установить его и не подстановка вместо фактического runtime SHA.

## Разрешённая операция и границы

Владелец разрешил явно обозначенный **синтетический публичный QA-курс** для
положительной проверки страницы базы знаний. Это не публикация работ учащихся.
Builder создаёт один новый3D-проект своего QA-класса, собственный private курс,
свой раздел и только текстовый material lesson, immutable published version и
whole-class run своего класса; затем открывает только этот QA-курс как public.
Класс/проект/курс имеют title с QA-label и случайным nonce. Никакие существующие
курсы, проекты, publication, profile или пользовательские роли не изменяются.

`publicationId` выбирает оператор явно: только существующий **PUBLIC** UUID.
Builder выполняет анонимные GET detail и exact revision artifact, проверяет
provenance/readOnly/supported status; не создаёт публикацию, не запрашивает
копирование/like/сообщения и не следует authorAvatarUrl или внешним media URL.

Реальный запуск только на каноническом Linux под root, с уже готовыми100 QA
seat-сессиями и полным операторским набором required log source IDs:

```sh
sudo python3 scripts/release_load_resources.py build --apply --canonical-root /srv/asa-lab --origin https://school.example --revision FULL_FINAL_RUNTIME_SHA --schema 294 --label QA-class-acceptance --fixture /var/lib/asa-qa/cohort.json --nonce NONCE_FROM_PRIVATE_FIXTURE --publication EXPLICIT_PUBLIC_UUID
```

Root/clean exact checkout/runtime SHA/schema/origin/Compose identity/volumes и
приватный файл проверяет тот же `HostAdapter`/`PrivateStore`. `resources` до build
содержит только classroomId; существующий mixed fixture не перезаписывается.
Когорта, teacher/admin IDs,100 seats, expiry и source IDs связаны отдельным
SHA256 binding; токены не продлеваются и не ротируются. Новый helper не вызывает
SQL, auth bootstrap, session выдачу, установку, restart или network changes.

До любой записи `/api/auth/me` подтверждает точные synthetic teacher email,
account ID, QA displayName, personal workspace и verified educator/content_author;
далее проверяется own personal class с точным
nonce-title/owner, **ровно100 fixture seat IDs**, единственный QA teacher и
отсутствие приглашений. Проверка состава повторяется непосредственно перед
whole-class assignment. При лишнем ученике, преподавателе, приглашении или
другой identity операция останавливается; сообщения реальным людям недопустимы.
Код класса остаётся только в приватном fixture. Dynamic audience штатной API
не замораживается этим инструментом: во время теста не менять QA membership и
не выдавать этот код реальным людям.

Каждый POST/PUT предваряется сохранением private intent. Случайные request IDs
фиксируются до запроса. Нет автоматического replay даже при штатной API
идемпотентности. Ответ проверяется как кандидат до изменения canonical state;
последний валидный snapshot используется для независимого cleanup. Лимит —140
плановых HTTP вызовов/5 минут плюс завершение последнего10-секундного запроса,
семь плановых API writes. Аварийный cleanup имеет отдельный предел20 вызовов/120с
и максимум один private PUT: полный предел одного build —160 вызовов/восемь writes. Неожиданная ошибка прекращает новые создания.

После writes проверяется readback собственного class project и публичного
knowledge course. У **всех100** учеников читается назначенный run и один material
lesson. В `resources.lessonId` попадает UUID **materialized run lesson**, а не
sourceLessonId автора. Все шесть IDs присоединяются к тому же private fixture
лишь после этих положительных проверок. `resourceEvidence` отмечает API
projections; это не PASS нагрузки, browser100, численной симуляции или выпуска.

## Аудит побочных действий до разрешённых записей

Аудит выполнен по committed Git blobs **0d693050fc75b9e5e040e00227a654b5f772e87a**,
который включает выбранные улучшения поверх main efe27e20. Чужой working tree
с незавершённой миграцией не изменялся.20 дополнительных файлов в
`profiles/school-mixed-resource-contract.json` закрепляют просмотренный путь:

| Путь | Проверенный эффект |
|---|---|
| `auth.controller.ts:payload/me` | Точный Account/email/workspace; session.observed остаётся внутренним analytics event, не письмом/сообщением |
| `projects.controller.ts` → `CreateProjectUseCase` → `pg-project.repository.ts:createWithDraft` | Новый Project/Draft, audit и classroom activity; локальный empty3D document, без email/HTTP provider |
| `courses.controller.ts` → `course_save_v2` (0111), `course_section_save` (0064), `course_lesson_save_v3` (0173→0137/0075) | Только собственный курс/раздел/lesson, owner/revision guards; paragraph block без media/assignment/activity references |
| `course_publish_v4` (0231→0111→0153) | Immutable snapshot/version и audit; не глобальная рассылка, profile не меняется |
| `classroom_course_run_assign_v4` (0127→0232) → `learning_audience_create/materialize_set` (0095) | Run + copied material lesson + enrollments только checked QA roster; dueAt=null, material kind, без activity blocks |
| `course_enrollment_children` (0109) и notifications (0120/0133) | Participation notification trigger относится к activity_participations. Здесь нет activity_runs: material-only path не создаёт participations. Нет submission/result/join mutation или reminder deadline; email/outbox/SMTP/webhook путь не вызывается |
| `course_visibility_set` (0061), gallery knowledge | Owner-scoped UPDATE только своего QA курса; gallery query требует visibility=public. Cleanup возвращает private тем же штатным API |

Существуют штатные **in-app notifications** в системе вообще; это не основание
называть их выключенными. Для выбранного material-only пути не найдены writes,
которые адресуют реальных людей, email/messages или внешний сервис. При смене
профиля на assignment/activity, реальном составе или source drift apply должен
остановиться, а путь — пройти новый аудит. SQL dependencies проверяются на
выбранном source snapshot, не являются универсальным доказательством будущих
миграций; окончательный runtime SHA и его schema выбирает оператор после review.

## Cleanup прежде истечения сессий

**Сначала** скрыть свой QA-курс; **потом** выполнить core fixture expiry.

```sh
sudo python3 scripts/release_load_resources.py cleanup --apply --canonical-root /srv/asa-lab --origin https://school.example --revision FULL_FINAL_RUNTIME_SHA --schema 294 --label QA-class-acceptance --fixture /var/lib/asa-qa/cohort.json --nonce NONCE_FROM_PRIVATE_FIXTURE
```

Cleanup заново подтверждает QA teacher identity, читает owner-only `/api/courses`,
требует единственный точный nonce-title и совпадение known course ID, затем
возвращает **только его** visibility=private и проверяет readback. Переименованный,
чужой или неоднозначный known course не считается отсутствующим/private. При
потерянном create receipt ID находится через тот же owner/title fence. Existing
PUBLIC publication никогда не меняется. Нет DELETE/DROP/archive или удаления
проектов, run, version, enrollments; все created IDs остаются в private intent.
Из активного `resources` убираются mixed IDs, чтобы повторная нагрузка после
cleanup отказала уже на fixture preflight.

При build failure best-effort private cleanup выполняется **до** записи failure
receipt, включая постоянный отказ диска. Потерянный public PUT может ещё
выполняться, поэтому private observation не означает settled outcome. Pending
intent сохраняет `PRIVATE_OBSERVED_INFLIGHT_UNKNOWN`/`UNKNOWN_OPERATOR_CLEANUP_REQUIRED`;
повторного create нет. После фактической проверки, что прежние собственные
запросы завершены, ручной cleanup требует `--attest-no-inflight`. Завершённый
cleanup идемпотентен. Ни один код ошибки не содержит exception/API/SQL body,
credentials или токенов.

Тот же teacher token действует максимум до исходного deadline≤2ч. API-only
cleanup **не может** войти после истечения/отзыва без новой authority. Он
останавливается `RESOURCE_SESSION_EXPIRED`, не создаёт/продлевает sessions и
сохраняет IDs для отдельного операторского восстановления. Планировать cleanup
нужно внутри этого окна, не после core expiry. Автоматический scheduler не
установлен; аварийная потеря хоста/неоконченный запрос остаются UNKNOWN.

## Проверенная корректировка source pins

64 route entries и31 route blobs сохранены. Просмотренные отличия от прежнего
snapshot14ca13ed и fixture baseline41359062:

- `public-projects.controller.ts`: nullable authorAvatarUrl, отдельный avatar
  GET и catalog/sort selection; прежние detail/publication/artifact contracts
  сохраняются. Avatar URL не запрашивается builder/runner.
- `creator-portal/navigation.ts`: query sort для той же Explore страницы; число
  страниц не меняется. Все варианты UI sort этим HTTP-тестом не доказаны.
- `ai-image-generation.ts`/`ai-image.service.ts`: approved queuePaused, fresh
  admission/single-GPU dispatch и безопасные worker diagnostics; runner limits
  не ослабляются, unknown/queued не превращаются в успех.
- `admin-logs.service.ts`: finite requiredCoverage/native integrity; прежний
  pinned required-source guard runner теперь получает выбранный API contract.
- `public-project-display-artifact.ts`: строгая allowlist arduinoBlocks; это не
  численная приёмка electronics.

**SSE `ai-chat.controller.ts` совпадает с прежним улучшенным hash e9823158… и не
перепривязан к bare efe без heartbeat.** Fixture изменён ровно один из11 pins,
route inventory — шесть из31; остальные побайтно совпадают. `sourceRevision`
инвентаря — наблюдаемый0d, а apply всегда требует переданный фактический полный
runtime SHA и точное совпадение каждого blob. Blind repin для обхода отказа
запрещён. Source alignment не означает deployment/CI/product acceptance.

Focused command: `python -m unittest discover -s tests -p 'test_release_load*.py' -v`.
Fake adapters проверяют API shapes, семь writes/100 positives, неизменённые
sessions/TTL/roles, лишних людей, lost response, broken receipts/disk, scoped
cleanup и transport guards. Живые API/DB и нагрузка не запускались. Реальный
изолированный ASA API здесь не собран: отдельный private app checkout/build/DB
fixture в этом infra CI не настроен; добавление cross-repository credentials
или application workflows не входит в срез. Linux CI подтверждает offline
contracts, а реальный операторский apply остаётся отдельным evidence.
