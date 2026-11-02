# H03: закрытый runtime-контракт forensic guard

Source-only кандидат для последовательности «штатное immutable update → остановка
исправленного собственного collector → явное recovery». Установка, восстановление,
остановка/запуск служб и новая проверка H03 этим срезом не выполнялись.

## Что разрешает guard

Прежние точные контракты сохранены: `dfee5badd299d318a8e51ff0cbf88e9656333a5d`
использует четыре файла, `3e32de6987665c4d7c1d33ba7ba82019afdf9cdb` — пять.
Отсутствие пятого файла не переключает пятифайловый контракт на четырёхфайловый.

Добавлен один закрытый пятифайловый контракт по фактическим runtime-байтам ASA
[PR965](https://github.com/spikeal8-maker/asa-lab/pull/965), immutable source
`6a98bddd8953eff732ec7d2cbb3de8cdec8abdee`. Его bundle SHA256:

```text
b5d79dbc1c97bf4d94226b4c2c06eac4795c7366ae1a9b17add83a46c0790731
```

Алгоритм не изменён: SHA256 последовательно получает UTF-8 имя относительно
`tools/`, затем точные bytes файла, в порядке `collect_logs.py`,
`collect-windows-logs.ps1`, `resource_sampling.py`, `memory_sampling.py`,
`deployment/operational.py`. Проверены Git object ID, размер и SHA256 всех пяти
immutable Git blobs; происхождение закреплено в
[source snapshot](../snapshots/2026-11-03/h03-forensic-fixed-runtime-contract.json).

Для иной зарегистрированной revision одновременно необходимы:

1. Строка из ровно 40 lowercase hex символов.
2. Зарегистрированный `scriptDigest`, равный единственному закреплённому bundle hash.
3. Повторное чтение **всех пяти фактических файлов** с сохранёнными no-follow,
   размерными и race-проверками; вычисленный hash должен совпасть с тем же pin.

Одного записанного `scriptDigest` недостаточно. Изменённый byte, другое окончание
строк, отсутствующий файл или новый самосогласованный hash блокируют новый контракт.
CLI/environment override и автоматического вывода неизвестного layout нет.
Merge/doc/helper-pin commit может иметь другую revision только при неизменных
пяти runtime-файлах. Это не общая совместимость с будущими выпусками и не отдельная
аттестация Git HEAD, application images, публикации или CI.

## Сохранённые границы

Сверки canonical root/project, registered owner/UID/GID, unit, manager/bus,
quiescence, конфигурации, закрытых путей и существующего lock не изменены.
USER runner не конвертируется в root, службы не запускаются и права не расширяются.
Guard остаётся PLAN по умолчанию и не исправляет history/квитанции/metadata.

Публичный SHA256 **LF-байтов** нового `scripts/diagnostic_forensic_snapshot.py`:
`f6ed00d8f2ba9d8011275d26907a0573421faf5c79119a534ca086a5fe456ff6`.
CRLF-копия имеет другой hash и не заменяет этот доверенный артефакт.
Независимое review guard и новая отдельная правка app helper, закрепляющая этот
guard hash/source, обязательны до использования: старый helper ещё требует 6b0.

## Проверки и ограничения

Windows: 24 теста, 8 PASS / 16 Linux-only SKIP. Новые Linux-сценарии проверяют
фактическое чтение всех пяти файлов, changed-byte/missing-file, поддельный stored
pin, неизвестный самосогласованный bundle и сохранение unit/quiescence guards.
Синтетический успешный bundle в этих Linux-тестах явно подменяет только константу
pin; он не выдаётся за реальные байты PR965. Производственный pin вычислен отдельно
из immutable Git blobs. Linux CI и независимое review — отдельные доказательства.

Ни новый guard, ни source-контракт не подтверждают восстановление H03, полноту
истории или покрытие всех источников. Запрет возобновления старого dfee после
успешного metadata repair и остальные требования recovery остаются в силе.
