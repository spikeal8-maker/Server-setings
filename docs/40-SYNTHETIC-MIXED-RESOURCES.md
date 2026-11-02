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
последний валидный snapshot используется для независимого cleanup. Лимит —260
плановых HTTP вызовов/10 минут плюс завершение последнего10-секундного запроса,
восемь плановых API writes, включая QA-class AI allow. Аварийный cleanup имеет
отдельный общий предел20 вызовов/120с и максимум два PUT (AI deny и own course
private): полный предел одного build —280 вызовов/десять writes. Неожиданная ошибка прекращает новые создания.

После writes проверяется readback собственного class project и публичного
knowledge course. У **всех100** учеников читается назначенный run и один material
lesson. В `resources.lessonId` попадает UUID **materialized run lesson**, а не
sourceLessonId автора. Все шесть IDs присоединяются к тому же private fixture
лишь после этих положительных проверок и AI-доступа всех100 QA seats. `resourceEvidence` отмечает API
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

**Сначала** восстановить AI deny своего QA-класса и скрыть свой QA-курс этой
cleanup-командой; **потом** выполнить core fixture expiry. Запрет и видимость
обрабатываются независимо: сбой одного не пропускает проверку/компенсацию другого.

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

## AI-доступ только синтетическому классу (дополнение к source alignment0489)

Новый `build` явно включает AI в собственном nonce-owned QA-классе. Для уже
подготовленного legacy mixed fixture без `aiAccess` существует отдельная команда:

```sh
sudo python3 scripts/release_load_resources.py ai-enable --apply --canonical-root /srv/asa-lab --origin https://school.example --revision FULL_FINAL_RUNTIME_SHA --schema 294 --label QA-class-acceptance --fixture /var/lib/asa-qa/cohort.json --nonce NONCE_FROM_PRIVATE_FIXTURE
```

Plan той же команды без `--apply` выполняет ноль чтений fixture/source и HTTP.
Разрешение не выдаётся молча: plan содержит `allowsOwnSyntheticClassAi:true`,
private intent — `mixedResources.aiAccess`. Команда требует прежний ready mixed
fixture, исходные сессии и deadline; повторный enable после любого intent запрещён.

Перед PUT повторяются проверки exact teacher/owner/nonce-title, ровно100 своих
seat IDs, единственного QA teacher и отсутствия invitations. Нормальный GET
`/api/ai/classrooms/:id/access` обязан показать active writable class,
`mode=deny`, `revision=0`, все100 override=inherit и canGenerate=false.
Исходный minimumRating сохраняется, без снижения порога. Затем один штатный PUT:
`{kind:"classroom",mode:"allow",minimumRating,requestId,expectedRevision:0}`.
Случайные разные allow/restore request IDs, исходные deny/revision0/minimumRating
и намерение компенсирующего deny предварительно записываются в тот же private
intent. Только точный `{saved:true,revision:1}` становится доказательством своей
записи; GET класса проверяется до и после чтения `/api/ai/scopes` от имени
каждого из100 реальных QA seat-cookie: собственные seat/class IDs,
canGenerate=true, reason=null и прежний minimumRating. Генерации не выполняются.

Cleanup восстанавливает deny только при exact ownership/100-roster/team fence,
сохранённом подтверждении собственного allow revision1 и неизменной текущей
policy allow/revision1/minimumRating/all-inherit. PUT deny использует заранее
записанный отдельный requestId и expectedRevision1; readback обязан показать
deny/revision2/100 denied. Любая intervening revision, override, membership или
minimumRating change прекращает policy write; CAS дополнительно защищает гонку
между GET и PUT. Собственный QA-курс всё равно независимо возвращается private.
Отказ записи receipt не мешает обеим best-effort компенсациям. Частичный fixture
теряет активные mixed IDs (все созданные IDs остаются в private intent).

Потерянный/невалидный allow PUT receipt **не доказывает**, что наблюдаемая
revision1 принадлежит этому инструменту: чужая CAS-запись может выглядеть так же.
Автоматического replay/усыновления revision1/перезаписи нет. Сохраняется UNKNOWN
и pending; ручной cleanup после проверки завершения запросов требует
`--attest-no-inflight`. Если собственной receipt недостаточно или policy
изменилась, возвращается STOP `RESOURCE_CLEANUP_OPERATOR_REQUIRED`: нужен
операторский разбор через обычное управление **своим QA-классом**, не SQL bypass.
Даже наблюдаемый deny/revision0 при ещё возможном inflight allow не считается
settled success. Сессии не продлеваются; разрешение класса **не имеет TTL**:
истечение teacher/seat sessions не заменяет восстановление deny. Cleanup надо
выполнить до исходных≤2ч и до core expiry; после этого helper не восстанавливает
authority. При SIGKILL/потере хоста/постоянном I/O всё остаётся операторским
UNKNOWN, автоматический scheduler не устанавливается.

Аудит перед authoring: `ai-access.controller.ts` проверяет teacher Account,
same-origin JSON и class UUID; `ai-access.service.ts` делегирует только
`ai_classroom_access_get/set` и `ai_access_scopes`; миграция0226 сериализует class
row, проверяет teacher/CAS/request receipt, меняет class policy и пишет внутренний
audit event. Email, сообщения другим людям, глобальные модели/provider config
или внешняя генерация этим путём не вызываются. Все3 chain blobs совпадают на
observed0489 и read-only2259; они **добавлены** к прежним20: теперь23 обязательных
source pins, старые20/11 fixture/31 route pins и64 route entries не менялись.
Фиксированный count23 проверяется до Git/HTTP; runtime SHA по-прежнему задаётся
оператором явно. Это source/fake-API proof, не live API/load100/release acceptance.
