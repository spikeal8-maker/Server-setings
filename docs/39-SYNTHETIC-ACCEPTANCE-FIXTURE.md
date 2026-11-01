# Приватная воспроизводимая QA-когорта

`scripts/release_load_fixture.py` готовит только минимальный **core** fixture для
[HTTP gate20/50/100](38-SCHOOL-HTTP-LOAD-100.md). Это исходный инструмент, а не
подтверждение живого создания или приёмки100 учеников. По умолчанию — план без
чтения fixture/ASA checkout, subprocess, сети, Docker или БД:

```sh
python3 scripts/release_load_fixture.py --origin https://school.example --revision FULL_40_HEX_SHA --schema 289 --label QA-class-acceptance
```

## Что создаётся при отдельно разрешённом apply

Только на каноническом Linux-сервере под root, без установки зависимостей:

```sh
sudo python3 scripts/release_load_fixture.py --apply --canonical-root /srv/asa-lab --origin https://school.example --revision FULL_40_HEX_SHA --schema 289 --label QA-class-acceptance --fixture /var/lib/asa-qa/cohort.json --required-source-id docker --required-source-id operations:updates
```

Набор `--required-source-id` в примере сокращён: оператор закрепляет **весь**
реальный непустой набор подключённых обязательных источников, включая DB,
контейнеры, OS, updates и настроенные providers/backup. Инструмент не выводит его
из текущих успешных источников и не делает missing источники необязательными.

Корень должен быть точным чистым checkout runtime SHA. До первой мутации
импортируется существующий `tools/deployment/system.py` именно из этого корня,
проверяются `Installation.identity()` (Compose project/profile, data volumes,
engine/database targets) и `readiness(SHA,schema)` (Web/API/Scratch), затем тот же
SHA/schema на выбранном HTTPS origin, который обязан точно совпадать с
`ASA_UPDATE_ENTRY_ORIGIN` канонической приватной `.env`. Произвольному иному
origin новые cookie не передаются. Профиль и приватная `.env` берутся штатным
менеджером; альтернативная БД или контейнер не создаются. Fixture дополнительно
связан с canonical root, Compose project и именами PostgreSQL/MinIO volumes.

`profiles/school-fixture-source-contract.json` закрепляет11 фактических Git blob
SHA256 auth/class/protected-login/password/resource API исходников. Изменение
контракта останавливает apply **до** регистрации/SQL. Обновлять хеши можно только
после просмотра соответствующего diff, а не для обхода `FIXTURE_API_SCHEMA_DRIFT`.
Наблюдаемый исходный контракт проверен по ASA checkout41359062; эти файлы могут
совпадать и на другом явно выбранном runtime SHA. Сам этот SHA не является
поручением установить выпуск.

Последовательность:

1. На диск до мутаций записывается приватный intent с новым случайным nonce,
   точными параметрами, временем и deadline через2 часа.
2. `auth_register_account` создаёт **два новых** аккаунта
   `asa-qa-<nonce>-teacher/admin@example.invalid`, private profiles и штатные
   личные workspace/tenant/principal/session. Уникальность email, username и
   нового QA display label проверяется до bootstrap, существующие записи не
   принимаются и не изменяются. SQL проходит только stdin локального `psql` в
   подтверждённом контейнере, не аргументы процесса или консоль.
3. Только эти новые аккаунты получают verified email и нужные fixture grants:
   teacher — educator/content_author, отдельный admin — platform_admin. Это
   **NOT_PROVEN для настоящей регистрации, почты и password-login**. SMTP и
   подтверждение чужих email не вызываются. Hash соответствует формату
   `scrypt-v1$16384$8$1$...`, но сформирован из случайных salt/digest без известного
   пароля/прообраза; пароль человека не используется и не сохраняется.
4. Teacher `POST /api/classrooms` создаёт собственный personal класс с title
   `<QA-label> <nonce>`, `ageBand:mixed`, `topicKeys:[]`, `safeModeDefault:true`.
   Проверяются owner/workspaceKind/title; сохраняется настоящий9-символьный
   ClassCode (в представлении `ABC DEF 234`).
5.100 последовательных `POST /api/classrooms/{id}/seats` с
   `{displayLabel:"<QA-label> pupil N",safeMode:true}` создают защищённые
   credentials штатным controller. Для каждого `POST /api/class-join/studentseat`
   с `{code:ClassCode,studentCode:StudentCode}` должен вернуть authenticated,
   точный seat/class и собственную cookie. Принимаются только реальные4–10
   символьные StudentCodes. Случайные32-символьные DB handles не используются
   как доказательство входа. Коды и cookie остаются только в приватном fixture.
6. Срок всех сессий этих новых аккаунтов/собственного QA-класса сокращается до
   единого deadline≤2ч. Успешный `state:core_ready` совместим с неизменённым
   runner `--profile core`; содержит100 различных seat UUID/token.

Рабочие аккаунты, роли, классы, курсы и публикации не используются для записи.
Новые сущности и штатный аудит остаются для разбора. Нет DELETE/DROP, изменения
схемы, очистки volumes, генерации AI, email/сообщений или создания public content.
Исходные project/course данные и существующие sessions не заменяются.

## Приватность, сбой и завершение

Родительский каталог fixture создаётся0700, файлы0600, атомарная запись и fsync.
Он должен находиться вне **любого** Git checkout. Symlink/reparse/`..`, hardlink,
чужой владелец и открытые POSIX права отвергаются; два исполнителя держат один
неблокирующий private lock. Live apply на Windows запрещён: permission bits не
доказывают NTFS ACL. Консоль выводит только закрытый status/error code, counts и
путь; никогда fixture/cookies/passwords/SQL error rows/HTTP тела.

Перед каждым внешним действием сохраняется `pending`. Автоматического replay
частичного создания нет: новый вызов provision не перезаписывает существующий
fixture и не создаёт повторные seats. При обычной ошибке выполняется best-effort
expiry только nonce-owned записей. Подтверждение относится к уже наблюдаемым
сессиям: при потерянном ответе API/SQL возможен ещё выполняющийся серверный запрос,
поэтому `OBSERVED_SESSIONS_EXPIRED_INFLIGHT_REQUEST_UNKNOWN` **не означает** завершение
этого запроса. При SIGKILL/потере хоста finalizer не гарантирован; родной API TTL
ещё не сокращённого join может превышать2ч. Нужны проверка отсутствия in-flight
и повторный own-expiry; до этого не запускать нагрузку или новую когорту.
Для partial/pending intent явная команда expiry требует `--attest-no-inflight`
после фактической проверки завершения прежних собственных запросов оператором.

После теста **и при его отказе** оператор выполняет:

```sh
sudo python3 scripts/release_load_fixture.py expire --apply --canonical-root /srv/asa-lab --origin https://school.example --revision FULL_40_HEX_SHA --schema 289 --label QA-class-acceptance --fixture /var/lib/asa-qa/cohort.json --nonce NONCE_FROM_PRIVATE_FIXTURE
```

Nonce читается оператором приватно, а не передаётся в чат. Expiry проверяет
binding, exact synthetic email/username, creator policy marker и owner/title
класса **до** UPDATE. Также найдёт nonce-owned class при потерянном create-ответе.
Bootstrap/expiry сериализуются общим SQL advisory lock. Expiry сокращает срок и
отзывает только сессии двух новых QA-аккаунтов/их класса, отзывает только новый
platform_admin grant с точным marker. Не удаляет аккаунты/класс/данные, не меняет
чужие grants. Повторный expiry допустим. Не обновлять runtime между тестом и
expiry: дрейф SHA/schema/source identity блокирует команду для разбора оператором.

**2ч — TTL сессий, не автоматический TTL capability grant.** В текущей схеме grant
не имеет `expires_at`. Отдельный scheduler не устанавливается. QA-admin не имеет
известного пароля; после истечения cookie доступа нет, но grant должен быть
явно отозван own-expiry. Коды собственных QA-seats также остаются приватными и
не ротируются expiry; повторный ручной вход создаст новую родную API-сессию.

## Оставшийся mixed resource plan — не исполняется этим инструментом

Все шаги ниже выполняются только отдельной проверенной API-последовательностью
с teacher fixture и новым QA title, после проверки актуальных shapes. Ни один
не применяется этой командой. Существующий школьный курс/публикация не меняются.

| Нужное поле | Штатная последовательность и shape |
|---|---|
| `classProjectId` | `POST /api/projects`, header Idempotency-Key UUID, body `{scope:"classroom",classroomId:QA_CLASS,module:"three-d",title:"QA-... classroom"}`; сохранить `project.id`; GET должен подтвердить class/scope |
| Собственный course | `POST /api/courses` body `{title:"QA-... course",summary:"Synthetic acceptance only",visibility:"private",requestId:UUID}` → `{id,draftRevision}` |
| Section | `POST /api/courses/{id}/sections` body `{title:"QA section",expectedRevision:LAST_REVISION}` → `{id,draftRevision}` |
| `lessonId` | `POST /api/courses/{id}/lessons` body `{sectionId:SECTION,title:"QA material",kind:"material",content:"Synthetic acceptance material",expectedRevision:LAST_REVISION}` → `{id,draftRevision}`. Только текстовый материал, без чужих media/assignments |
| Immutable version | `POST /api/courses/{id}/publish` body `{expectedRevision:LAST_REVISION,requestId:UUID}` → `versionId/versionNumber`; не игнорировать prepublish errors |
| `courseRunId` | `POST /api/classrooms/{QA_CLASS}/course-runs` body `{courseId:COURSE,versionNumber:VERSION,audienceType:"whole_class",seatIds:[],requestId:UUID,timeZone:"Europe/Moscow"}` → `runId`; GET ученического `/api/class-join/me/course-runs` обязан содержать этот run и lesson |
| `publicCourseId` | Это **не** private published course: `/api/gallery/knowledge/{id}` явно фильтрует `visibility='public'`. Нужна отдельная проверка публичного QA контента; после разрешения — `PUT /api/sharing/course/{OWN_COURSE}/visibility` body `{visibility:"public"}`. Не ослаблять runner или видимость существующего курса |
| `publicationId` | Либо явно выбранный оператором существующий **PUBLIC** ID только для GET, либо отдельный собственный QA personal проект через `POST /api/projects` `{scope:"personal",classroomId:null,module:"three-d",title:"QA-... publication"}`. Для новой публикации сначала штатно сохранить непустой draft+snapshot, проверить owner publication status, затем отдельно разрешённый `POST /api/projects/{OWN_PROJECT}/publication` body `{title:"QA-... publication",description:"Synthetic acceptance only",tags:["qa"]}`; читать полученный `publicationId` из status. Публичная операция может иметь дополнительные policy/profile требования; не подменять их SQL и не менять реального пользователя |

`gallery.controller.ts` доказывает необходимость global public visibility для
knowledge route. `courses.controller.ts` доказывает named API bodies/revision
guard и material lesson; `public-projects.controller.ts` — normal publish body и
раздельный artifact read. Их canonical blob hashes входят в source contract.
Это source proof формы API, **не** успешный integration run и не подтверждение
отсутствия notifications при будущем назначении/publication. Эти side effects
тоже проверить до разрешённого выполнения. Глобальные/public действия не имеют
скрытого apply-флага в provisioner.

После создания/проверки **собственных** ресурсов можно безопасно дополнить тот же
private fixture через `attach-resources --apply` с теми же binding/nonce и шестью
`--resource KEY=UUID`, плюс `--attest-own-qa-resources`. Существующий PUBLIC
read-only publication дополнительно помечается `--publication-existing-public`.
Например набор ключей: `classroomId`, `classProjectId`, `publicationId`,
`publicCourseId`, `courseRunId`, `lessonId`. Чужой class UUID, неизвестные поля,
неполный набор, пустой pinned log source set и истёкшая когорта блокируются.
Это только явная операторская аттестация происхождения ID, не автоматическая
проверка ownership каждого course/project: `resourceEvidence` остаётся
`OPERATOR_ATTESTATION_ONLY_API_ACCEPTANCE_NOT_RUN`. Сам runner затем проверяет
точные положительные API projections. Не меняются seats/accounts/tokens,
не продлевается TTL, не создаются ресурсы и не присваивается mixed PASS.

## Проверки исходников

```sh
python3 -m unittest discover -s tests -p 'test_release_load*.py' -v
python3 -m compileall -q scripts tests
```

Только offline fake adapters:100 различных синтетических IDs,
строгие codes/session receipts, partial intent, failure expiry/unknown, приватный
диск, no-read plan, redaction, SQL ownership fences и закрытый attach. GitHub
`Synthetic release HTTP gate contracts` запускает их на Linux; локальный Windows
пропускает POSIX mode/lock тест. **Live bootstrap/SQL/protected code creation и
load20→50→100 остаются NOT_RUN**, пока оператор не выполнит reviewed source на
каноническом сервере. Нет local Docker или рабочей DB проверки этой разработкой.
