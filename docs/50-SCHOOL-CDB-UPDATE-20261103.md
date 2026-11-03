# H03 → School: cdb rollout и ограниченная квалификация 03.11.2026

Это операторские доказательства **TEST qualification существующих установок**,
не production acceptance, новая чистая ASA installation или полная системная приёмка.
На обоих узлах использован один immutable deployment:
`sha256:b1c6c45780df6e56011cd1eb5fbd19d54e6c880b2e6300b685f38f7f97e106ec`,
source `cdb77ed6ac003ef2eb4635b9f55c4eb1c9a35bfa`, schema 300.
Exact source gate/build/publish прошли в [Portable release run 41605174970](https://github.com/spikeal8-maker/asa-lab/actions/runs/41605174970).
Это публикация deployment artifact, не доказательство полной runtime-приёмки.
Полные приватные receipts сохранены оператором. Здесь нет объектных ключей,
приватных файловых путей, policy bodies, конфигурационных значений или backup payload.

## Последовательность и сохранность данных

H03 сначала обновлён штатным менеджером: operation
`56fd8ae77a1644f9a8db301fb516213d` SUCCESS, затем независимый readback
`3085fd806ed84b068e5a8f4b3f5c9185`. Private proof SHA256
`bde2f71e7ed2bd03442ef28e346fcac5e8665caa034e8cddbe0e04a5cee8cca3`.
Сохранены пять business-table full-row fingerprints, mounts и private settings 0600.
Независимый scope — deployment/data/host-provider component, без fresh all-source
health или full-system acceptance. Хронология: [909 — H03 checkpoint](https://github.com/spikeal8-maker/asa-lab/issues/909#issuecomment-6608150188).

School исходно работал на `de1efb9b45a0ee10233cab53a066cb0c8c4f121b`, schema 286.
Перед switch подготовлен root-controlled external cdb tooling: 45 raw Git files,
manifest SHA256 `3ed688798f954877f75380c79ef5f4e5c479954b2c653276eb4d33f8f7fdf19b`.
Native CHECK подтвердил доступный exact release без изменения приложения.
Первый staging observer завершился FAIL из-за сравнения atime при чтении manifest,
хотя native CHECK был успешен. Отдельный reviewed continuation проверил source
повторно, исключив только atime из CAS; первый FAIL и его лог сохранены.

Свежая School rehearsal завершена `2026-11-03T16:02:08.384315Z`:
backup `20261103T160112Z-8781bf73`, **2314 verified files**; штатный restore
в новую изолированную test DB, migration 286→300 и повторный no-op PASS.
Все пять full-row fingerprints сохранены:

| Таблица | Rows |
| --- | ---: |
| accounts | 96 |
| users | 7 |
| projects | 2211 |
| project_drafts | 2211 |
| classroom_student_seats | 513 |

MinIO content fingerprint, старый live runtime/containers/private configuration
при rehearsal не менялись. Это изолированная PG rehearsal плюс проверки MinIO
backup/inventory, а не запуск отдельно восстановленного MinIO server.
См. [909 — fresh rehearsal](https://github.com/spikeal8-maker/asa-lab/issues/909#issuecomment-6608878782).

Actual School update выполнен reviewed wrapper SHA256
`d9f3339c30c9a283b74be8af7324902d81a15083259ee1bac9f4fe09c6968102`.
Первый запуск остановился до native manager: отсутствовал отдельный operator work
directory. После создания только этого root-private каталога выполнен отдельный
continuation с тем же frozen wrapper; первый лог сохранён.
Сначала source-bootstrap без collector-mode flags получил только ожидаемый
`RETRY_NEW_MANAGER`: новый source cdb, прежние runtime/data, тот же pending digest.
Только после проверки пяти canonical runtime files выполнена вторая native phase
с явным `system-root`, reader GID 1000 и тем же immutable pin. Canonical manager
подтвердил UPDATED cdb/schema 300 и running system collector.

Перед switch сам менеджер создал **другой**, свежий native backup
`20261103T161220Z-a810ca10`: **2320 verified files**, manifest SHA256
`fa87d733a83ac96341abe8c4c10b68d84ea8944109a2295000cd0cc6716ae8bc`.
Поздние observers отдельно подтвердили те же пять full-row fingerprints,
persistent volumes, original private keys и settings 0600; в settings допустим
только явный выбор root collector, в private environment — только проверенные
добавления без изменения исходных ключей.

## Original FAIL сохранён, MinIO различия проверены отдельно

Wrapper завершился `FAIL_INSPECT_PRESERVED_STATE / MINIO_CONTENT_CHANGED_INSPECT`
в `2026-11-03T16:13:10.748119Z`, уже **после** native update/readiness и сравнения
business tables. Original receipt SHA256
`c4dcb78423662b347f887e020c895adef0a71d8853958650e11954187e12cf8b`
не изменён. Менеджер повторно не запускался, rollback/restore автоматически не выполнялись.
См. [909 — update и original storage guard](https://github.com/spikeal8-maker/asa-lab/issues/909#issuecomment-6609324968).

Отдельный read-only audit `16:18:41.118144Z`, SHA256
`aa2c1e618c083dee281209722d316a96fe402e74ac929eac4a8f3dfc49417152`,
проверил все 2320 backup files и identity текущего School MinIO volume.
Все business namespace paths/raw bytes совпали. Из 2312 backup object files и 2311
текущих: missing 8 / added 7 / modified 7, только внутренний namespace MinIO.
Различия: temporary files 7/7, scanner/usage metadata 6 modified, bucket metadata 1
modified и удалённый пустой 4-byte MRF header. Пустой header соответствует pinned
MinIO startup contract; это не потерянная очередь операций. Внутренний prefix
сам по себе не использовался как blanket exception.

Bucket metadata дополнительно декодирована из двух raw-SHA-bound XL2 файлов.
Semantic receipt `16:28:43.862278Z`, SHA256
`9dcaa0a341c3e7dd61c5f42701e38c7295c660032f33ccf91bbb08b5cfb0a5be`:
из 25 exported fields **только `PolicyConfigUpdatedAt` отличается**.
Все 11 configuration payloads byte-equal, включая policy/encryption/versioning/
lifecycle/replication/targets; identity/Created/LockEnabled сохранены.
PolicyConfigJSON отсутствует до и после. Проверены raw SHA bindings, inline size
и MD5 ETag. Native XL2 xxhash и HighwayHash bitrot этим decoder не проверялись.
См. [909 — semantic proof](https://github.com/spikeal8-maker/asa-lab/issues/909#issuecomment-6609663567).

Отдельная independent composed review приняла **deployment/data scope** на основе
этих раздельных proofs. Она не переписала original FAIL или frozen v2 observer:
у последнего остаются `STORAGE_SEMANTICS_PENDING` и
`bucketMetadataLogicalEqualityQualified=false`. Позднее composed решение —
отдельный ограниченный результат, не новый статус старого файла.

## Последняя конечная inspection, не бессрочная гарантия

Actual read-only inspection at `2026-11-03T16:41:39.358779Z`:
private receipt 13730 bytes, SHA256
`94be0fcb012f843d1a91034dc5fe6555a975398ffd93960a596474e8103bd39c`;
status `POSTUPDATE_INSPECTION_COMPLETE_ORIGINAL_FAILURE_PRESERVED`.

- Loopback и HTTPS entry возвращают cdb/schema 300; Scratch reader entry OK.
  Это HTTP/resource checks, не GUI login/save/reopen journeys.
- Root collector: PID 3727086, system-root, reader GID 1000, autostart enabled;
  cycle state ok, heartbeat 11.253s, catalog 16.197s, cycle age 16.187s.
  History audit 244/244 complete, incomplete=false — snapshot, не universal coverage.
- Более ранний v2 snapshot at 16:34:57: cycle 3.704s / heartbeat 9.065s, S3 HeadBucket
  accessible, policy absent, те же пять fingerprints/volumes/keys, settings 0600.
  Приватные buckets/policy bodies не публикуются.
- Relay определён из actual unit/process: три metadata GET reachable,
  Ollama tags 659ms (5 models), ps 704ms (1 resident), ComfyUI stats 760ms (1 device).
  Генерация/качество моделей не проверялись; direct GET не заменяет continuous monitor.
- Источников 45: state ok 40, pending 4, unavailable 1 (Windows, non-applicable).
  Docker lifecycle среди ok имеет possible gap. Число ok не означает qualified coverage.
  Required policy содержит 23 IDs, но observed migration/minio-init/search recent
  не имеют трёх declarations, хотя их текущие read windows были около 20s.
- AI usage pending, through age 39.313s; historical audit/classroom/product analytics
  pending. Native persisted state ok age 123.043s / drop 0 / pending 0; live memory queue
  **NOT_OBSERVED**, старый persisted snapshot не доказывает текущее состояние очереди.

## Открытые границы

School provider-monitor не установлен; fileSources пуст, operationalSources не
настроены. Same-host ingress observation не аттестована: edge — другой узел.
PR68 ephemeral CI проверил USER integration, но не положительный canonical
system-root case. В ROOT branch выявлена несовместимость с допустимым runner
UID:GID 0:1000 и unit Group=1000; эта интеграция имеет статус
**SOURCE_REPAIR_PENDING**. Это не отказ работающего School core collector.
PR981/982 не установлены; исправления required declarations и ingress time остаются
source/release dependencies. PR977 уже исправлен в source main, но не входит в cdb:
использованы restrictive umask 077, отсутствие generic writer temp и postcheck 0600,
без chmod/permission waiver для подавления отказа.

Не доказаны global all-source coverage, continuous remote GPU raw-log collection,
authenticated module journeys, real generation, 100 users, Electronics 836 closure,
production/full-system acceptance или новая clean full ASA installation.
Автоматический business-data backup не заменяет отдельную custody/export истории журналов.
В этом docs-only slice не выполнялись host changes или CI reruns.
Машиночитаемый snapshot: `snapshots/2026-11-03/school-cdb-update.json`.
