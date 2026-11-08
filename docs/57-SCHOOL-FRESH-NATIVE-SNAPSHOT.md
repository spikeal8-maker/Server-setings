# Архив настроек при чистой установке через 1Panel

## Статус и граница

Это отдельное исправление воспроизводимости для будущего развёртывания школьного
профиля. Оно подготовлено и проверено локально. Реальный прогон на чистом Linux
с 1Panel — **NOT_RUN**. Этот документ не подтверждает публикацию, установку,
облачную копию или восстановление нового релиза на сервере.

Принятый текущий переход школьной VM использует Server-setings
`eb21b1bbb8852c38a3c5b4d9ed6cfa3f72fa58f4`, snapshot helper `2a0b49e5816aa6d145c717b33de4061c7ea65e39c48d59d93db2b80cf93fbea2`
и immutable settings release `52887551f6bf27e9f73ff2ba6b754f082b2bbec19dd4ef59813ce287ab7b04e0`.
Его исходники и действующие операции этим локальным кандидатом не заменяются.
Существующие legacy unit-файлы на этой VM позволяют завершить принятый переход
без данного исправления.

## Исправляемая несовместимость

Режим native Cron `--fresh` требует отсутствия старых пар service/timer.
Предыдущий snapshot helper, однако, безусловно включал в обязательный список
`asa-log-collector-asa-lab-school.service` и пару
`asa-school-nightly-database-backup.service` / `.timer`. Поэтому корректная новая
установка останавливалась при чтении отсутствующего файла; архив без этих
файлов также отклонялся проверкой manifest.

Формат старого архива и список разрешённых путей сохранены. Старые архивы,
содержащие legacy-файлы, проходят прежние проверки. Два набора legacy-файлов
теперь можно пропустить только целиком и только при доказанной соответствующей
native-конфигурации:

- Для collector обязательны полный набор bridge/config/INI, точный опубликованный
  bridge, выбранный manager `1panel-supervisor`, правильные instance/project/root,
  запись принятого handover с исходным `LoadState=not-found`, совпадающие текущие
  settings/runner/log configuration и точные параметры native процесса.
- Для database service/timer обязательны полный набор native schedules,
  `ACTIVE` / owner `1panel` / originalOwner `none`, отсутствие исходных legacy
  пар, правильные instance/project/generation, согласованные bytes/SHA/path
  immutable wrapper, обе native INI и два различных принятых gated probe.

Одна оставшаяся часть пары, состояние `STAGED`, незавершённый handover, чужая
identity, изменённый bridge/wrapper, другой command или неподтверждённый probe
не разрешают пропуск. Удаление legacy-файлов после обычной миграции не является
поддержанным способом перейти в fresh-профиль: исторический handover с
`LoadState=loaded` и schedules с originalOwner `legacy` не принимаются как
свидетельство чистой установки.

## Проверка при создании и восстановлении

Проверка владения выполняется по уже прочитанным байтам конфигурации при
создании архива и повторяется по извлечённым файлам при изолированной проверке
восстановления. Код из архива не импортируется и не исполняется. Для проверки
нет обращений к 1Panel API, запуска сервисов или исправления настроек.

Отсутствующие legacy-пути попадают в наблюдаемый набор как отсутствующие. Если
файл или symlink появляется во время snapshot, операция останавливается.
Проверки прав, symlink, bounded reads, hash, SQLite integrity и normalized ZIP
сохраняются. Секреты остаются внутри зашифрованного архива; raw logs и private
collector index по-прежнему исключены. Полнота исторического покрытия не
заявляется.

Helper не закрепляет будущий SHA native wrapper в собственном коде: это создало
бы цикл, поскольку wrapper закрепляет SHA helper. Проверяется согласованность
архивных wrapper bytes, ownership state и имени immutable release, а INI
сопоставляются с фиксированным контрактом. Публикация и установка wrapper
по-прежнему требуют отдельно проверенного Git-источника и его точных pins.

## Зависимости и проверка

Новый helper требует нового immutable settings release. В этом кандидате
обновлены только соответствующие публичные pins у settings runner, installer
и native Cron wrapper. Существующие разрешённые SHA legacy settings service
сохранены; добавлен точный SHA service, который сгенерирует новый installer.
Использовать legacy installer для native-only установки не требуется.

Рецепты управления остаются в [документе collector](55-SCHOOL-1PANEL-LOGGING.md)
и [документе native backup schedules](56-SCHOOL-NATIVE-BACKUP-SCHEDULES.md).
Чистая установка должна сначала принять collector, затем выполнить stage и
оба gated probe, принять native ownership и только после этого создавать
архив настроек. Никакой скрытый systemd collector или backup timer этим
исправлением не создаётся.

Локальная проверка из корня репозитория:

```sh
python3 -B -m unittest discover -s tests -p 'test_school*.py'
```

Проверены положительный synthetic fresh-native tar → isolated restore,
совместимость старых архивов, отклонение неполных наборов и чужого ownership,
привязка двух INI/marker identities, появление legacy-файла во время snapshot,
а также точное соответствие pins байтам всех зависимостей. Это доказательство
контрактов кода, не результат реального развёртывания или live restore.
