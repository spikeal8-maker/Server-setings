# H03: отдельная репетиция successor recovery на свежем клоне

Issue #909. Только source-кандидат operator harness; H03, исходная forensic-копия,
службы и рабочие данные этим PR не изменялись. Это не новая lineage и не разрешение
лечить live журнал. Перед применением обязательны независимый review точного SHA
и отдельное решение контроллера о репетиции.

## Закрытые входы

- App helper и полный import closure из **6a98bddd8953eff732ec7d2cbb3de8cdec8abdee**.
  Восемь точных LF-normalized SHA256 проверяются ДО исполнения любого кода:
  helper, collector, deployment package/paths/operational, resource/memory sampling,
  database-diagnostic-contract JSON. Никакие установленные dfee-модули не импортируются.
- Отдельно предоставленный root-owned forensic guard из reviewed **6b0eb3d**,
  LF-normalized SHA256 `5dd13624af168db3a3d1eb14eb0e9762506eb626dcccd102fe858dd266542e1e`.
  Его новый release-compatibility вариант не входит в эту привязку.
- Сохранённый HOST набор `/var/backups/asa-forensics/h03-host-20261102-909`, manifest
  `eccb0bf480fd636f1d527da4a4b78e024668d2f4e34dbc8e6f600d25481889d5`.
  Он остаётся `NOT_A_VERIFIED_RECOVERY_SNAPSHOT`, а не становится здоровым backup
  из-за успешной репетиции. Его индекс открывается только immutable/read-only.
- Ожидаются строго **31 predecessor / 607 records / 1 authenticated successor**.
  Другая группа данных отклоняется. Это incident-specific инструмент, не произвольный
  portable restore для будущих выпусков.

Root Linux, Python stdlib, `-I -B`, доверенные root-owned родители без group/world
write; private родитель назначения 0700. Код/guard можно отдельно разместить в
проверенном root-owned staging. Инструмент не скачивает исходники и не вызывает
Git, Docker, systemctl, loginctl, shell, сеть, Collector constructor/cycle/publish.
Для кода проверяются все зависимости и отсутствие конфликтующих import modules.

## PLAN без записи

Ниже пути staging являются примерами; контроллер сначала проверяет точные артефакты.
Единственный разрешённый родитель назначения — `/var/backups/asa-rehearsals`; он должен существовать с root:root 0700;
само назначение должно отсутствовать. Никакого повторного использования каталога.

```sh
sudo python3 -I -B /var/lib/asa-programme909/diagnostic_successor_rehearsal.py \
  --app-source /var/lib/asa-programme909/app-6a98bddd \
  --guard-tool /var/lib/asa-programme909/guard-6b0/diagnostic_forensic_snapshot.py \
  --snapshot /var/backups/asa-forensics/h03-host-20261102-909 \
  --destination /var/backups/asa-rehearsals/h03-successor-909-attempt-01
```

PLAN проверяет approved helper PLAN, точный manifest/receipt и полный инвентарь,
включая отсутствие лишних файлов, затем повторно проверяет исходную authority.
Каталог назначения не создаётся. Вывод содержит только закрытый статус, версии,
SHA и агрегаты; ключи, ID, имена сегментов и payload не выводятся.

## Отдельный APPLY только рабочего клона

После independent review добавить **`--apply`** к той же команде. Это не live APPLY.
Инструмент создаёт новый root0700 каталог; каждый файл создаётся exclusive 0600,
никаких hardlinks, symlinks, reuse или overwrite. HOST raw файлы копируются полностью,
пустые каталоги сохраняются; рабочий index берётся из standalone SQLite backup,
а не из raw index/WAL/SHM/journal. Native application journal исключён границей
исходного HOST snapshot и не читается из действующего приложения.

Перед writable SQLite open и до commit выполняется approved `SQLiteSidecars`
проверка плюс pinned index identity. В клоне сначала выполняются три настоящие
негативные проверки transaction core: изменённый record body, неполный target set,
исключение перед commit после записи. Каждая обязана завершиться отказом без
изменения **любых** SQL таблиц/schema. Затем вызывается approved analyze/apply_metadata
для полной группы; измениться могут только `retired:metadata` и отдельный typed
ledger `retired:successor-recovery:v1`. Все прежние записи этих ключей сохраняются,
вся остальная БД (events, receipts, retired markers, source errors и другое),
каталог, ключ и все иные скопированные bytes проверяются на неизменность.

После закрытия writer открывается новое immutable/read-only соединение; его
SQLite backup переносится в `:memory:`. На свежем `Collector.__new__` вызываются
**реальные fixed-runtime retained-history validation methods**, typed-ledger и
проверка всех retired файлов. Constructor, upgrade, finish_compaction, cycle и
publish не вызываются. Возможные audit/receipt изменения от validation остаются
только в памяти и отбрасываются. Проверяются весь active каталог, все retired
сегменты и полные счётчики index, а не только 607 recovered records. Последние
отдельно перепроверяются точным approved analyze после применения.

Исходная forensic-копия и runtime source closure перепроверяются ДО выдачи
success receipt, в том числе при ошибке. Файлы и каталоги клона fsync до закрытой
`rehearsal.private.json`; private receipt не содержит ключей/строк журналов.
Частичный/неудачный каталог сохраняется, не удаляется и не используется повторно.
`FAILED.private.json`, отсутствие success receipt или ненулевой exit означают
**FAIL/INCOMPLETE**, даже если metadata в disposable клоне уже была записана.
При kill/power loss может не быть FAILED marker: отсутствие подтверждённого
success receipt также означает INCOMPLETE. Новый запуск требует нового назначения.

## Значение результата и ограничения

`OFFLINE_REHEARSAL_PASSED` означает только этот проверенный metadata-only эксперимент.
`historyHealthy=false`, `liveApplied=false`, `fullCollectorStartup=false` остаются
явными. Это НЕ full Collector startup/cycle, НЕ восстановленная live coverage,
НЕ разрешение возобновлять старый dfee collector и НЕ school acceptance.
Реальная установка fixed release, порядок pause/update/repair/resume, полный
runtime startup и квалификация живой истории являются отдельной reviewed процедурой.

Source-проверки: `python -m unittest discover -s tests -p test_diagnostic_successor_rehearsal.py -v`.
Локально Windows: 10 PASS, 5 POSIX-only skips; Ubuntu CI обязателен для dirfd/no-follow
семантики. Дополнительный локальный synthetic probe на точном approved 6a98 closure
выполнил analyze, три negative core transactions, apply и cold validation: 257/257
published rows, 226 active и 32 retired сегмента; bytes source DB не изменились от
cold validation. Это synthetic evidence, НЕ репетиция на сохранённых данных H03.
