# H03: восстановление истории журналов — операторские доказательства 03.11.2026

Статус: **фактические операторские доказательства; независимое review требуется**, не полная системная приёмка. Только H03;
школьный сервер и бизнесданные этим документом не изменялись. Полные приватные
команды/результаты остаются у оператора; ключи, payload и идентификаторы событий
здесь не публикуются. Агрегаты ниже переданы контроллером из фактических квитанций.

## История и запуск фиксированного collector

- Защищённая forensic-копия сохранена. PLAN подтвердил 31 retired segment,
  607 записей и один аутентифицированный successor; независимая prerequisite
  проверка — `49d492790ade49559be42937e3b253ba`.
- Metadata-only APPLY: `803b0437436741b5a03fed64e393e023`. Независимое сравнение
  `89ea167f4af443718757a842522e9bb9`: 160891 events, все non-state таблицы/schema
  и 1395 файлов неизменны; изменены только две разрешённые state keys.
  Legacy debt=0, typed successor ledger=1. Оригинальные HMAC не подделывались.
- Ingress configuration CAS/ownership proof `11aab3b456564591b4b24378c380ae0b`:
  UID1000/mode0600. USER fixed collector start `6048b26aadf24770b0f0207fbe44b16d`:
  PID1246904, NRestarts=0. Старый runtime после metadata repair не возобновлялся.
- Первый полный audit: 1092/1092 COMPLETE; после обычной compaction — 445/445
  COMPLETE. Поздняя квитанция `8fdbacfef88342b28d330174cc8643c0`: 453/453
  COMPLETE, incomplete=false, catalog age19.2s, heartbeat5.0s, тот же fixed PID,
  NRestarts=0. Это фактические snapshots, не бессрочная гарантия.

## Доставка и свежесть

- HTTP canaries `9d38c687d0174c55a5c11e3b00a4ef32`: portal/Scratch/Electronics
  accepted202, duplicate202, invalid400, unauthenticated admin401, ready200.
- Native integrity `2ce0bfdb9d454cada5972290faa39e85`: 12253 required segments
  verified; 43 accepted/43 persisted, 0 dropped/0 pending, каждый canary один раз.
- Host published raw-SHA proof `91be31963610401ab977fc36e80f1505`: четыре сегмента;
  все три canary и внешний ingress canary встречаются по одному разу.
- Metrics `1f965abc9932413db7ad8c9977f1e799`: проверены raw SHA34 сегментов;
  три periodic samples 11:11/11:12/11:13 имеют полное покрытие целей,
  missed/dropped/expired/quota=0, publish lag16.732/2.846/12.251s.
  `host_commit_pressure` warning сохранён, не скрыт.
- Ingress `1e6f8a52e8874c7384d9c1c3cf73440a`: 265/266 строк имеют полный NGINX
  combined format без ведущего ISO timestamp. Collector использует file_mtime;
  исторические строки нельзя считать визитами за последние15 минут.

## Границы и оставшиеся вопросы

### Поздние API/storage/export проверки

- Контролируемый synthetic HTTP429 context принят202, повтор202, дополнительное
  недопустимое private field отклонено400. Storage proof
  `1bedf8b16a6b46a08ac7e1f78cc385d2`: ровно одна electronics/error запись,
  httpStatus429 и контекст сохранены, dummy field отсутствует; writer dropped0,
  pending0. Это тест доставки диагностики, не реальный инцидент HTTP429.
- Фактический compiled API reader `08a7632ee7a64a45a6c4f988800bb7eb`:
  query1/page1/export1, ZIP1794 bytes, SHA256
  `bfafa1e49f884ecaf1f9bc3a6d5933de3e7cd1cb6b29df35ec7f13218329047b`;
  foreign owner denied, download verified. Независимая проверка
  `b3ba5ec3af9d4f07ae5092a46796210b`: CRC/normalized manifest/context верны,
  search text не экспортирован, coverageComplete=false сохранено.
- Из API-контейнера три provider metadata GET200
  (`5716023e37f2420e80d8254ccdc7c044`), задержки34/20/7ms; генерации не было.
- Read-only AI inventory: семь image jobs complete; за7 дней80 succeeded,
  16 cancelled, 2 failed. Это существующие данные, не новые тестовые генерации
  и не доказательство качества изображений.
- Через приватный SFTP intake сохранены24 GPU wrapper logs, всего2223 bytes.
  Это отдельная сохранная выборка, **не** квалифицированная непрерывная remote
  raw-log collection. GPU-сервисы не перезапускались.

### Source-кандидаты и provider exporter

App PR981 `fcbe414e27d4610c13b1723262a05790817c91f3` (Compose required sources)
и PR982 `339bf66f7b44d7158cc2ebf789f5ec871b7e7764` (Nginx timestamps) получили
независимый source APPROVE и scoped Ops proof, но не установлены. General имеет
отдельный baseline formatting blocker в чужом `PublicationOwnerActions.spec.ts`;
это не General PASS и не разрешение live app update. Новые migration301/303
не входят в операторский срез восстановления журналов.

Infra provider exporter PR66 source
`5553a56fdcb3a10829339d6fae6cb7bb3d88cd4a` принят обычным merge
`1b2c7d407d5dd005490e9d0777ec1f69e8eb0892`; все5 точных CI checks SUCCESS.
Финальный фактический root synthetic proof25 PASS/0 skips:
`1d66baecb66b4ef5bd6aca488f4d0701`. PLAN и unit verification:
`f5db5eaa0b6a4d63b56dff04a7a4543e`. Пробы с неподходящими output permissions
ранее отклонены до записи; guard для `/var/log` mode0775 не ослаблен.

Фактический oneshot и timer enable — `c6aebc4a319742e086a637bc6233119e`, без
генерации и перезапуска app/GPU. Root output `/var/lib/asa-provider-monitor`:
root:GID1000, directory0750/log0640/latest0600. Два samples разделены121.143s,
все endpoints reachable, check age13.323s (`a6818be4db13484aaef0529ea5c2f8ed`).
USER config CAS `8e7a7b756cbb43ada6ed47a08fa4706e` добавил только provider-monitor
fileSource, сохранив UID1000/mode0600; config SHA256
`a68f996c9de788b231b91f1929f187a673ca66cf6f1c4e4438954fe955091d6d`.
Fixed USER collector перезапущен для конфигурации: PID1246904 →1382415.

Поздняя actual-delivery проверка `d2dda12d77c54ea6911f93c2a31ac561`: root6
samples, опубликованы5; samples старше40s представлены ровно по одному,
raw SHA и receipt fields совпадают, timestamp basis=event, last publish age72.388s,
source OK. **Availability checking не квалифицирует application provider policy**:
`applicationProviderPolicyQualified=false`. Это не утверждение, что AI policy
выключена, и не разрешение использовать какую-либо модель.

Ранний source snapshot: 46 sources, 40 OK и6 non-OK (четыре historical DB
pending, aggregate pending и historical Docker lifecycle gap). Текущие required
AI usage и Linux journal после catch-up OK. API requiredCoverage отдельно
неполное: отсутствуют declarations для search/migration/minio-init. Исправление
policy — отдельный source slice, пока не объявляется установленным.

После настройки monitor `3c01dd0f875b4addba3e06573c1ad83c`: audit457/457 COMPLETE,
heartbeat9s, catalog12.6s, sources47/41 OK; остальные bounded AI/historical DB
pending и Docker historical gap остаются видимыми. Это отдельный поздний snapshot,
а не обещание вечной свежести.

Root monitor availability доказана, но application provider policy этим не квалифицирована; continuous
GPU raw collection по-прежнему не доказана. Нет утверждения universal historical coverage, all-modules/100-user
приёмки, Electronics836 closure или школьного обновления.

Ранние operator probes не являются product defects: неверный `/api/health/ready`,
неверная сигнатура native verifier и сравнение compressed hash вместо raw hash
исправлены последующими правильными проверками. Их failures не стираются и не
выдаются за успешные проверки.

Документ фиксирует переданные оператором квитанции на момент проверки; позднейшее
независимое подтверждение может быть отдельным review comment без переписывания истории.
Машиночитаемый snapshot: `snapshots/2026-11-03/h03-log-recovery.json`.
