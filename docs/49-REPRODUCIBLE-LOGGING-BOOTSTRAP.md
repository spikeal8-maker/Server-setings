# Переносимый bootstrap provider monitor и подключение журналов

Это reproducible **logging component**, не installer всего ASA и не приёмка новой
БД/MinIO, реального GPU, модели, all-modules/100 users или Electronics836.
H03 incident metadata repair не является шагом новой установки.
Нужны Ubuntu24.04/systemd, Python3.12+, Git и утверждённый доступ к GitHub;
bootstrap не устанавливает пакеты, не меняет порты/сеть и не вызывает Docker.

## 1. Сначала стандартная ASA installation

Выберите один canonical root/project/profile и существующего оператора. Выполните
штатный `tools/asa_manager.py install` из отдельно квалифицированного app release
согласно его `docs/deployment/PORTABLE_OPERATIONS.md`. Поддерживаемые install/update
prepare/reconcile собственный collector; один `docker compose up` этого не заменяет.
Root `system-root` — только явный заранее выбранный режим с root-controlled source;
обычный USER collector остаётся USER, не получает новый root broker.

Проверьте native API storage и host collector отдельно: это разные хранилища с
разными лимитами (app2a native7d/128MiB, host default30d/1GiB). На целевом сервере
должны быть собственные registration/root/unit/UID/GID, readiness и process identity.
Private `.asa/installation.json`, `collector-runner.json`, `log-collector.json`
должны быть single-link regular files, owner:primaryGID mode0600, без symlink и
delegated-write ancestors. Интегратор отказывает при ином состоянии.

App main checkpoint `bbf905d1` ещё не включал private writer PR977;
он мог повторно сохранить installation.json с неверным mode. Source fix теперь
merged `0fc52994c6b390e566a2fb7f7e957e605763599f`; это не qualification combined
published release и не H03 update. Не обходить отказ: нужен qualified app release
с исправленным writer и повторная private-mode проверка.
PR981/982 source-only fixes required-coverage/ingress timestamps тоже не считаются
установленными этим bootstrap. Не выдавать component CI за исправление ASA release.

## 2. Выбрать и получить reviewed infra source

Один раз выберите reviewed полный infra commit SHA, реальную существующую
ненулевую reader group (не Docker socket group) и numeric private provider origins.
Никаких IP/incident paths H03 в defaults нет. На новом узле root может выполнить
следующие команды после настройки собственного GitHub read access без токенов в
командах/репозитории. Source target должен отсутствовать; чужой checkout не трогать.

```sh
sudo -i
umask 022
read -r -p 'Reviewed full infra commit SHA: ' INFRA_SHA
test "${#INFRA_SHA}" = 40
test ! -e /var/lib/asa-provider-bootstrap-source
git clone --no-checkout https://github.com/spikeal8-maker/Server-setings.git /var/lib/asa-provider-bootstrap-source
git -C /var/lib/asa-provider-bootstrap-source checkout --detach "$INFRA_SHA"
test "$(git -C /var/lib/asa-provider-bootstrap-source rev-parse HEAD)" = "$INFRA_SHA"
read -r -p 'Existing collector reader GID: ' READER_GID
read -r -p 'Ollama numeric origin incl actual port: ' OLLAMA_ORIGIN
read -r -p 'ComfyUI numeric origin incl actual port: ' COMFYUI_ORIGIN
python3 -I -B /var/lib/asa-provider-bootstrap-source/scripts/bootstrap-provider-monitor.py install --reader-gid "$READER_GID" --ollama-origin "$OLLAMA_ORIGIN" --comfyui-origin "$COMFYUI_ORIGIN"
```

Например `http://127.0.0.1:11434` допустим только если провайдер реально уже
слушает этот адрес/порт. На новом сервере с иными портами вводятся его origins;
bootstrap не перенастраивает провайдер, ASA DB или firewall и не угадывает адреса.
Origin — только canonical numeric private/loopback HTTP(S), явный порт, без
credentials/DNS/path/query/fragment. Никакого generated traffic.

PLAN не создаёт файлов/tempdirs/locks, не делает HTTP/DNS или systemd calls.
`applicationDownloads=3` означает **план** скачивания. Читаются source manifest,
проверяемые файлы, metadata существующих targets; offline bundle, если указан,
проверяется чтением. Loaded systemd collisions проверяются только при APPLY.

## 3. Установить повторяемо, без ручного копирования модулей

```sh
python3 -I -B /var/lib/asa-provider-bootstrap-source/scripts/bootstrap-provider-monitor.py install --reader-gid "$READER_GID" --ollama-origin "$OLLAMA_ORIGIN" --comfyui-origin "$COMFYUI_ORIGIN" --apply --activate
# Успешная повторная команда проверяет те же bytes/config и возвращает NO_CHANGE;
# уже активный timer не перезапускается.
python3 -I -B /var/lib/asa-provider-bootstrap-source/scripts/bootstrap-provider-monitor.py install --reader-gid "$READER_GID" --ollama-origin "$OLLAMA_ORIGIN" --comfyui-origin "$COMFYUI_ORIGIN" --apply --activate
python3 -I -B /var/lib/asa-provider-monitor-runtime/provider_health_exporter.py check --config /etc/asa-provider-monitor/config.json
systemctl status asa-provider-monitor.timer asa-provider-monitor.service --no-pager
```

APPLY автоматически скачивает ровно три immutable Git raw app2a blobs (таблица
в [48](48-ISOLATED-PROVIDER-MONITOR.md)), fixed raw.githubusercontent.com HTTPS,
без proxy/redirect, с length/SHA256 и bounded HTTP reads.
Каждый полный transfer от DNS/connect до TLS/body ограничен12s killable subprocess;
на timeout дочерний процесс уничтожается и reap, без фонового DNS worker.
Онлайн нужен только при первом install; повторный install использует проверенные установленные bytes.
`--offline-bundle /ROOT_CONTROLLED/tools` вместо download читает те же три exact
files `deployment/{__init__,paths,operational}.py`; USER checkout не подходит.
Нет runtime зависимости на H03 incident bundle или mutable app imports.

Manifest закреплён length/SHA256 внутри bootstrap и перечисляет unchanged555
exporter, исходные service/timer templates и три app2a blobs. Для нового bootstrap
ровно два литерала `/opt/asa-provider-monitor` в service меняются на
`/var/lib/asa-provider-monitor-runtime`; весь полученный unit отдельно pinned
(870 bytes, SHA256 `4df7fc4a7ff38dab83a844791f4f18da5173f2c8f881f9ac007e459f2dfa2d4b`).
Это одна закрытая проекция без operator path guessing/normalization; timer и
shared PR66 templates остаются byte-identical. Config строится из явных inputs:
`/etc/asa-provider-monitor/config.json` root0600; код и bundle под
`/var/lib/asa-provider-monitor-runtime`, root-controlled; output `/var/lib/asa-provider-monitor`
root:readerGID0750, log0640/latest0600. Manifest/права/ссылки проверяются до
исполнения installed exporter PLAN; self source trust начинается с independently
reviewed Git commit, выбранного оператором, а не с хеша, присланного сервером.

Создание только отсутствующих файлов через O_EXCL/O_NOFOLLOW, fsync и собственный
lock. Существующие source/config/receipt должны полностью совпадать. Другой origin,
GID, файл, symlink/hardlink/drop-in/unit fragment или partial install — STOP без
overwrite/rollback cleanup. Сохраняются partial artifacts для отдельного review;
bootstrap не является updater/config editor существующей установки. Изменение
origins после install требует отдельного reviewed config CAS, не удаления receipt.
Только `--apply --activate` вызывает daemon-reload/start своего oneshot и
enable своего timer. Другие units/app/GPU не перезапускаются.

## 4. Подключить file source как существующий ASA owner

После AVAILABLE audit выйдите из root shell и используйте существующего
registered ASA owner с его настоящим user bus (для USER режима). Не запускайте
USER integration через sudo/root. Root mode разрешён helper только для полностью
root-controlled system-root installation. Подставьте реальные canonical inputs:

```sh
read -r -p 'Canonical ASA root: ' ASA_ROOT
read -r -p 'Existing Compose project: ' ASA_PROJECT
python3 -I -B /var/lib/asa-provider-bootstrap-source/scripts/bootstrap-provider-monitor.py integrate --root "$ASA_ROOT" --project "$ASA_PROJECT"
python3 -I -B /var/lib/asa-provider-bootstrap-source/scripts/bootstrap-provider-monitor.py integrate --root "$ASA_ROOT" --project "$ASA_PROJECT" --apply
```

Helper не импортирует/исполняет код ASA. Он проверяет canonical registration,
owner/private metadata, raw digest всех5 runner files, local unit shape/argv,
отсутствие local drop-ins, project/config limits и реальное чтение root journal.
Добавляет **только** `{"source":"provider-monitor","path":"/var/lib/asa-provider-monitor"}`
в fileSources; остальные JSON values сохраняет, metadata/history не пишет.
Совпадающая запись даёт NO_CHANGE без переписывания файла; другая/повторная запись
с тем же source даёт STOP. Apply блокирует config inode, перед заменой проверяет
CAS всех трёх private metadata files; atomic temp0600/fsync сохраняет owner/GID.
Одновременные config writers не допускаются; после CAS-конфликта новый PLAN.

Receipt выдаёт unit/mode/restartRequired и `loadedUnitVerified=false`: local unit
file не доказывает identity загруженного процесса. Через штатные runner checks ASA
проверьте actual loaded fragment/argv/root/owner и отсутствие runtime drop-ins.
Только после этого выполните явный restart **ровно выданного owned unit**:

```sh
# USER: имя берётся из checked helper receipt, не из неподтверждённого JSON.
read -r -p 'Checked collector unit: ' COLLECTOR_UNIT
systemctl --user restart "$COLLECTOR_UNIT"
# Для проверенного root system-root unit вместо --user выполняет root.
```

Обычный same-release reconcile не обязан замечать изменение config при прежнем
source digest/identity; он **не заменяет** этот явный restart. Helper не делает его
сам и не меняет collector mode, ingress, `.env`, release, volumes или private keys.
При уже совпадающем config `restartRequired=null` означает UNKNOWN: NO_CHANGE
не доказывает, что ранее добавленный source уже загружен текущим процессом.

## 5. Cross-host logging acceptance и история

- Проверить readiness приложения, native writer accepted/persisted/dropped/pending,
  host heartbeat/catalog/audit completeness и source IDs отдельно, без false green.
- Дождаться как минимум двух настоящих timer samples; `check` требует reachable
  обоих IDs, exact latest/log/config, реальный возраст всех timestamps0..120s.
- Через actual collector/API reader проверить published raw SHA, time=checkedAt,
  event timestamp basis, severity и каждый sampleId ровно один раз после bounded
  delivery lag. Event ID для повторной обработки должен совпадать, backlog/gaps
  остаются видимыми. Pure consumer CI не заменяет этот actual-reader шаг.
- `provider-monitor` tailer OK не означает provider availability, генерацию/качество
  или app required-provider policy. Нужны отдельные audit/coverage checks; ingress
  file_mtime не считать историческим временем визита; GPU raw continuity отдельно.
- До миграции сохранить штатный PG/MinIO backup/private `.env` и keys вне GitHub.
  **ASA backup исключает оба diagnostic stores.** Отдельно экспортировать retained
  normalized logs через поддерживаемый authenticated API/UI: default7d недостаточен
  для всей истории; выбрать явные интервалы≤31d, проверить ZIP CRC/count/SHA и
  source-health snapshot. На destination создать собственную registration, а не
  копировать старые root/unit/PID. Quiesce ровно его collector и как его owner
  выполнить `python3 "$ASA_ROOT/tools/collect_logs.py" --root "$ASA_ROOT" --once --import-history /PRIVATE/verified-normalized.zip`.
  Затем resume owned collector и проверить сохранённые IDs/idempotency, counts,
  query/export и freshness. Import не переносит private cursors/original receipts,
  HMAC authority/source health; forensic custody и incident recovery отдельно.

CI workflow `provider-monitor-bootstrap.yml` использует новый GitHub Ubuntu VM,
actual systemd и только loopback mock providers: PLAN/no writes, автоматический
Git raw fetch, apply+same-bytes/inodes repeat, offline repeat, настоящий второй
timer sample, nonroot integration/CAS boundaries и exact2a pure consumer. Это
logging-component proof; clean full ASA/DB/MinIO install и real GPU не запускаются.
Точный успешный CI head фиксируется в PR после фактического результата.

Первый CI source7a71: Ubuntu/Windows offline PASS, root new11 PASS, unchanged
exporter24/25 PASS, PLAN fixture FAIL; install step SKIPPED (run41636884119,
job125920211436). Git archive задаёт0664 files, root tar сохраняет archived modes:
новые ephemeral staging trees теперь явно лишаются group/other write до root
execution. Runtime/source guards не ослаблены; исходный failure не скрыт.
Следующий dba067 CI root11+exporter25 PASS; actual bootstrap PLAN отклонён
`DIRECTORY_TRUST` до target writes (run41637155430/job125921120254).
Ephemeral parent ownership/mode inventory добавлен для точной диагностики;
права production guards не ослаблены. Отдельно full-transfer deadline теперь
охватывает DNS через killable worker; native controlled-DNS-stall regression
проверяет timeout и отсутствие оставшегося процесса.
Следующий40b CI дал точный parent inventory: hosted Ubuntu `/opt` root:root0777,
остальные проверенные parents включая `/var/lib` root0755; PLAN закономерно
отказал. Global permissions не меняются: новый source/runtime и CI fixtures
размещаются под trusted `/var/lib`. Старые H03 units/config не мигрируются;
существующая manual installation блокирует bootstrap как partial/collision.
