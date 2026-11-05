# H03: репетиция восстановления диагностической истории

Дата отчёта — 3 ноября 2026, Europe/Moscow; времена операций ниже указаны в UTC 2 ноября.
Проверялся исходный Ubuntu `admins-MS-7D22`, существующая установка `/srv/asa-lab`,
project `asa-lab-dev`, вход `https://test.asa-lab.ru`. Школьная VM100 — другой узел.

## Доказанный результат

В 22:28:05–22:28:30 UTC на **новом отдельном клоне** прошла репетиция
восстановления метаданных 31 retired-сегмента: 607 уникальных записей точно
подтверждены одним уже аутентифицированным successor. Исходная защищённая копия
и рабочий индекс не изменены. Три проверки отказа подтвердили откат транзакций.

| Проверка всей сохранённой истории | Результат |
| --- | --- |
| Активные / retired-сегменты | 854 / 535 проверены |
| Все строки индекса | 156886 |
| Published / unpublished | 150787 / 6099, сохранены |
| Допустимые изменения клона | Только `retired:metadata` и `retired:successor-recovery:v1` |
| Остальные таблицы, schema, квитанции, catalog/error и исходные файлы | Проверки неизменности прошли |

Cold verification повторно открывает SQLite read-only, копирует её в отдельную
in-memory БД и вызывает методы проверки точного исправленного Collector.
Она охватывает всю retained history, но **не запускает constructor, cycle, publish
или реальную службу**. `fullCollectorStartup=false`, `liveApplied=false`,
`historyHealthy=false`: постоянный сбор и глобальная полнота этим не приняты.

## Точные исходники и выполнение

- Harness: [PR62](https://github.com/spikeal8-maker/Server-setings/pull/62),
  `0a7dd4abbe8bfd0f1d8bcccfc1c4558dc252435e`, независимое source APPROVE.
- Исправленный core и все восемь закреплённых файлов import closure:
  app `6a98bddd8953eff732ec7d2cbb3de8cdec8abdee`.
- Исходный HOST manifest:
  `eccb0bf480fd636f1d527da4a4b78e024668d2f4e34dbc8e6f600d25481889d5`.
- Stage: operation `53e45a0e391242f0af8b68058a6ab1aa`, 10 публичных файлов,
  259164 байта; проверены хеши, код помещён вне каталога приложения.
- Реальные Linux unit tests: `37678c902dce4e66bf6f8595b6dbf49f`,
  **15 PASS, 0 ошибок, 0 пропусков**.
- Read-only PLAN: `028823d2b45645afb4237dc86a83f67a`, точные 31 / 607 / 1.
- Репетиция: `11f9e2a463fb419a851dd9c5739dc476`, 24.592 секунды.

Приватный клон сохранён в
`/var/backups/asa-rehearsals/h03-909-0a7dd4abbe8b-20261103`.
Повторный apply в тот же каталог запрещён: инструмент не переиспользует и
не перезаписывает destination. Набор
`/var/backups/asa-forensics/h03-host-20261102-909` остаётся forensic reference,
а не автоматически пригодным для замены более нового рабочего индекса.

В offline-прогоне использовался закреплённый guard `6b0eb3d...` для чтения
снимка. Отдельный новый guard из [PR61](https://github.com/spikeal8-maker/Server-setings/pull/61),
`c3a362f32f8f5efdf63257517a28b8f8f1bf1791`, получил source APPROVE и
прошёл **29 Linux-тестов без пропусков** на H03, operation
`32974a9574f4436caceb1a9936d52192`.
Его LF SHA256:
`c82a304985a775a14861c8f76f40816487e15b51fbb51be82f1d7864bb2c1335`.
Он закрепляет два полных профиля точного пятифайлового runtime: Git LF и
canonical checkout с CRLF PowerShell. В каждом проверяются размер и SHA256
каждого файла и общий raw digest. Другие будущие runtime не объявлены поддержанными.

## Пробел ingress-логов и подготовка

Read-only audit обнаружил `fileSources=0`: проект OpenResty не входит в Docker
filter приложения. Оба фактических файла vhost доступны зарегистрированному UID1000
с его прежними группами — operation `bc102afe15614e87972cbf69fd19c562`.
Проверка journald этим действием не выполнялась.

Вне active config подготовлен private candidate, operation
`360a43b147724ca180239aca325890dc`: source `ingress` указывает на каталог
`/opt/1panel/www/sites/test.asa-lab.ru/log`, включая access/error и их ротации.
Candidate SHA256 `0ec332468f1a2a3429a7dd3ec26c82f2f52a5858e314eba5b38828efd69c06fe`;
исходный config `7b03afcb040f9b0c68a16cb20f8e5c6bf41b610c3bc12f74fd63aec59dca0514`,
UID/GID1000, mode0600. Candidate **не активирован**. При применении нужны CAS
исходных байтов и сохранение владельца/прав. Глобальная полнота остаётся PARTIAL.

## Оставшийся порядок

Нужны final helper pin нового проверенного guard, полный CI и immutable publication.
Затем — штатное обновление существующего H03 с сохранением данных и identity
USER-сборщика; остановка уже исправленного runner; live metadata recovery;
возобновление исправленного runtime и отдельная проверка свежести/полноты источников.
Старый dfee нельзя возобновлять после успешного metadata repair.

После квалификации — тот же immutable release на VM100, затем реальные страницы,
Scratch, электроника, 3D и AI, progression 20 → 50 → 100 на каждом узле.
Новое обновление приложения, live recovery и приёмка100 этой программой
**ещё не выполнены**. Цель5000 не является измеренным результатом.

Несекретный [snapshot](../snapshots/2026-11-03/h03-diagnostic-working-clone.json)
содержит закрытые счётчики и operation IDs; payload, ключи и рабочие данные
остаются в приватном хранении.
