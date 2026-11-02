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
   размерными и race-проверками. Для каждого файла отдельно проверяются закреплённые
   длина и SHA256 из source snapshot; общий digest также должен совпасть с pin.

Одного записанного `scriptDigest` недостаточно. Изменённый byte, другое окончание
строк, отсутствующий файл или новый самосогласованный hash блокируют новый контракт.
CLI/environment override и автоматического вывода неизвестного layout нет.
Merge/doc/helper-pin commit может иметь другую revision только при неизменных
пяти runtime-файлах. Это не общая совместимость с будущими выпусками и не отдельная
аттестация Git HEAD, application images, публикации или CI.

Общий legacy digest не кодирует границы файлов. Независимое review отвергло
первоначальный guard `6297fd1`: в точных bytes PR965 перенос хвоста
`collect_logs.py` от boundary token на offset74486 в `collect-windows-logs.ps1`
меняет длины на74486/68641 и сохраняет общий b5d79 digest без hash collision.
Новый контракт отклоняет это перераспределение по отдельным file pins.
Legacy dfee/3e алгоритм и регистрационные контракты не изменены.

## Сохранённые границы

Сверки canonical root/project, registered owner/UID/GID, unit, manager/bus,
quiescence, конфигурации, закрытых путей и существующего lock не изменены.
USER runner не конвертируется в root, службы не запускаются и права не расширяются.
Guard остаётся PLAN по умолчанию и не исправляет history/квитанции/metadata.

Публичный SHA256 **LF-байтов** нового `scripts/diagnostic_forensic_snapshot.py`:
`e983f77a37aa4da6f03caf0d9bc3a449ab24a5f10afd5d3c751e8616f0c34961`.
CRLF-копия имеет другой hash и не заменяет этот доверенный артефакт.
Независимое review guard и новая отдельная правка app helper, закрепляющая этот
guard hash/source, обязательны до использования: старый helper ещё требует 6b0.

## Проверки и ограничения

Windows: 27 тестов, 10 PASS / 17 Linux-only SKIP. Новые Linux-сценарии проверяют
фактическое чтение всех пяти файлов, same-length changed-byte/missing-file,
перенос через filename delimiter при равном aggregate, поддельный stored pin,
неизвестный самосогласованный bundle и сохранение unit/quiescence guards.
Синтетические успешные fixtures явно подменяют ожидаемые bundle и per-file pins;
они не выдаются за реальные байты PR965. Производственные pins сверены отдельно
со всеми immutable Git blobs. Полный `registered_runner` также проверен в памяти
на исходных и реально перераспределённых PR965 bytes: старый guard принимает оба
набора, новый принимает только исходный. В этом challenge подменены платформенные
FD/metadata adapters; это не Linux permissions evidence. Linux CI и независимое
повторное review — отдельные доказательства.

Ни новый guard, ни source-контракт не подтверждают восстановление H03, полноту
истории или покрытие всех источников. Запрет возобновления старого dfee после
успешного metadata repair и остальные требования recovery остаются в силе.
