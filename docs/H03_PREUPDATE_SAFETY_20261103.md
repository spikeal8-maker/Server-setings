# H03 и VM100: проверка команды обновления и страховочная копия

Это операторская контрольная точка 3 ноября 2026 года. Времена ниже — UTC.
Новое приложение не установлено, live diagnostic recovery и тест100 не выполнены.

## Где выполнено

| Узел | Каноническая установка | Действующий runtime |
| --- | --- | --- |
| Исходный Ubuntu H03, `admins-MS-7D22` | `/srv/asa-lab`, `asa-lab-dev`, production | `dfee5badd299d318a8e51ff0cbf88e9656333a5d`, schema299 |
| Школьная VM100, `asa-school-01` | `/srv/asa-lab`, `asa-lab-school`, production | `de1efb9b45a0ee10233cab53a066cb0c8c4f121b`, schema286 |

Windows D2-R2 использовался для оркестрации и частного анализа артефактов.
Локальный Docker для этих проверок не запускался. GitHub Actions выполняет
отдельную изолированную квалификацию; она не заменяет проверку этих двух узлов.

## Команда на старой установке

Из точного объединённого app candidate
`2a28cffe7a20302f9b7231ef823bdc05f3fe43d0` подготовлена минимальная closure17
публичных Python-файлов. Каждый проверен по raw Git SHA256 до запуска;
пакет находится вне установленного приложения. На H03 он принадлежит root,
каталоги0750:GID1000, файлы0640:GID1000: оператор читает код, но не меняет его.

H03 operation `eb8b5287ba4a4be5a08944d6f287e8d4`, 00:40:06–00:40:10:
реальный UID/GID1000 запустил `asa_manager.py check` с canonical root/profile
и digest `sha256:289c8321c15d3795273a10aa0269430614aa44473d558f4186464a7ba335fd29`.
Exact Docker RepoDigest, извлечённый manifest, публикация и CI проверены;
результат `NO_CHANGE`, существующий dfee/schema299. Конфигурация и состояния
восьми контейнеров не изменились; maintenance/failed-switch/selector не появились.

Первый staging attempt `060f7e051e5848cd8b3cafd8ada05772` остановился до
исполнения кода: umask убрал group-read/traverse. Исправлены только права
отдельного публичного пакета; затем получен успешный результат выше.

Та же команда на VM100 от её штатного root прошла через существующий Proxmox
guest transport. Результат `AVAILABLE dfee/schema299`, установленный de1/schema286
не менялся; защищённые config bytes и состояния контейнеров неизменны.
Это проверка bootstrap/selector на уже опубликованном старом выпуске,
**не проверка доступности нового исправленного2a release** и не update.
На обоих старых узлах collector source contract честно показан unavailable;
это не объявлено здоровой историей журналов.

`check` не запускает backup, миграцию, source switch, collector reconcile или
service restart. Он допускает Docker image-cache, временный остановленный
network-none manifest container с удалением, временные файлы и Git fetch/index.
Полный вывод остаётся частным операторским свидетельством.

## Свежая бизнес-копия H03 и реальное восстановление

Operation `1a4af57e09a64a3998e5d4a8622605fd`, 00:41:43–00:42:08,
создала `/srv/asa-lab/backups/20261103T004145Z-52a9e054` штатным manager от UID1000.
Manifest SHA256 `6035845a2038605d131a7ae41d1cb4a9ef80f5a6ec3e5443f6912e0662dc489d`;
2207 payload files, 212360086 bytes. Архив содержит приватные данные/секреты и
остаётся в защищённом каталоге; в GitHub опубликованы только закрытые счётчики.

Штатная процедура остановила своих писателей, экспортировала PostgreSQL и MinIO,
проверила dump/inventory и возобновила прежние контейнеры/образы с ожиданием
dfee/schema299 readiness. Полная команда заняла23.897s; точная длительность
недоступности отдельно не измерялась. Maintenance marker отсутствует,
конфигурация, container IDs/images и шесть core-table fingerprints не изменились.

Operation `7f190a4d001145748aa5bd51d5a2d1bc`, 00:43:36–00:43:47,
повторно проверила весь файловый manifest и реально восстановила новую
`asa_preupdate_20261103_52a9e054_test` в существующей PostgreSQL.
Ни одна существующая БД не перезаписана и не удалена. ACL/RLS, schema ledger и
object checksums прошли штатный restore-check; тестовая БД сохранена.

| Core table | Строк | Отпечаток восстановленного набора совпал; live неизменен |
| --- | ---: | --- |
| accounts | 95 | PASS |
| users | 6 | PASS |
| classrooms | 32 | PASS |
| courses | 2 | PASS |
| projects | 1717 | PASS |
| project_drafts | 1717 | PASS |

Это не отдельный MinIO-server restore/S3 journey и не проверка всех UI.
Диагностическая история исключена из business backup и сохраняется отдельно;
защищённый forensic original не заменялся и не выдаётся за готовый rollback.
Владелец сообщил, что другой ранней копии diagnostics ему не известно.

На VM100 read-only `asa-school-backup-now --status` подтвердил зашифрованный
Яндекс-архив `asa-school-01-20261103T002057Z-4757cad2.tar.age`, verified
00:21:19.299375Z. Расписание daily03:20 Europe/Moscow включено; backup не
перезапускался этой проверкой. Ключи не передавались на сервер или в GitHub.

## Текущий кандидат и границы

Приняты source PR965 (retirement), PR968 (immutable selector), PR970 (legacy status).
Native 3-case run41546649313 прошёл три выбранных сценария, но его исходная
квитанция остаётся FAIL_OR_INCOMPLETE из-за следующего report-intake failure.
Новый independently reviewed reader8b подтвердил сохранённый исходный corpus;
его H03 Linux operation `bc59eb39b53d4ac38476fdccb2bb4b34` дала13PASS/1
явный isolated-GitHub-only skip из14 тестов. Runtime повторно этим не запускался.

Полный strict-real-time R4 запущен один раз для точного2a:
[run41554307177](https://github.com/spikeal8-maker/asa-lab/actions/runs/41554307177),
branch `qualification/final-2a28cffe-20261103`, head_sha проверен.
На момент этой контрольной точки он IN_PROGRESS; его результат сюда не выдуман.

Далее нужны exact-final CI и реальная immutable publication, migration rehearsal
на отдельной восстановленной БД, штатное обновление H03, остановка уже
исправленного сборщика, metadata recovery и возобновление только исправленного
runtime. Затем квалификация того же release на VM100 при закрытом blocker836,
проверка страниц/модулей и progression20→50→100. Цель5000 не измерена.

Несекретные результаты закреплены в
[snapshot](../snapshots/2026-11-03/h03-preupdate-safety.json) и
[операторской квитанции issue909](https://github.com/spikeal8-maker/asa-lab/issues/909#issuecomment-6592319759).
