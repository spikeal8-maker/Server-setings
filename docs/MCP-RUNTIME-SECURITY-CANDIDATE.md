# MCP: отдельный кандидат исправления runtime CVE

Source-only срез от main0985052. Никакие действующие образы, службы, настройки,
OAuth state, ключи, полномочия full или установщики не изменяются. FRP, rclone,
OpenResty и школьный runtime находятся вне этого среза.

Фактический baseline: общий образ шлюзов24b665…ed25 —53 исправляемые HIGH/CRITICAL
(13 Debian, включая4 CRITICAL, и40 Go). Отдельно монтируемый официальный mcp-1panel
SHA256427a6f…abc7d —4 HIGH stdlib1.26.6. См. docs/45-RUNTIME-INVENTORY-FINDINGS.md.

## Состав кандидата

`security/mcp/pins.json` закрепляет исходные upstream SHA, Go1.27.3 и linux/amd64
manifest digests официальных golang/debian images. Dockerfile применяет прежний
reviewed prepare-mcp-release.py/RFC9728/DCR patch без изменения общего patcher,
поднимает только выявленные gateway Go modules и пересобирает официальный
mcp-1panel с тем же исходным SHA и новым toolchain. GOTOOLCHAIN=local запрещает
неявную загрузку другого компилятора. Отдельный Debian snapshot20261102T000000Z
фиксирует apt universe; inherited packages также обновляются. Пакетный набор
curl/python3/pip/nodejs/npm сохранён для совместимости existing stdio commands.

Dependency resolution использует закреплённый upstream и точные запросы Go MVS;
фактически выбранные go.mod/go.sum и `go version -m` входят в артефакт. Это не
заявление о byte-for-byte одинаковом Docker image ID: provenance и метаданные
слоёв могут различаться. До принятия артефакта сравнить фактические версии с pins.

## Обязательный CI gate

Workflow `MCP security candidate (no deployment or registry push)`:

1. Клонирует только публичные pinned upstream sources, применяет существующие patches.
2. Выполняет Go tests обоих upstream и race tests OAuth packages; собирает image и ELF.
3. Запускает фактический candidate image с separately mounted candidate ELF,
   синтетической 1Panel API fixture, случайными временными OAuth credentials и TLS.
   Проверяет discovery/challenge/S256, точные11 tools, три подписанных API чтения,
   повторное использование существующего access token после рестарта и refresh,
   если сервер выдал refresh token. Тест не выполняет write tools.
   Дополнительно переключает только синтетический upstream на непрозрачный
   расширенный `asa_admin_execute`: listing и вызов должны проходить без
   whitelist по11 официальным tools. Fixture возвращает UID0/executed=false,
   никаких команд ОС не выполняет. Root-capable полномочия не урезаются.
4. Trivy0.76.1 с проверенным SHA сканирует фактический image и отдельный rootfs ELF.
   Gate требует supported Debian12, оба Go binary targets в image и отдельный
   mcp-1panel target с обнаруженными packages. Пустой scan не считается PASS.
   Fixable HIGH/CRITICAL или EOSL завершают gate ошибкой. DB metadata сохраняется.
5. Сохраняет candidate image tar, ELF, locks, hashes и scan reports как CI artifacts.
   Registry push, live credentials и deployment отсутствуют.

## Границы доказательства и следующий шаг

До реального успешного workflow это **подготовленный, непроверенный кандидат**.
Source/unit tests не подтверждают отсутствие runtime CVE. Новый scanner DB может
добавить findings и заблокировать кандидата. Unsupported/unfixed/medium/low и
host packages данным gate не принимаются как исправленные.

После CI нужен независимый review exact SHA/imageID/ELF hash. Только отдельный
deployment с backup/rollback может заменить два gateway images и существующий
host-mounted ELF; volume identities/OAuth keys/state/env/full mode и loopback
bindings сохраняются. Smoke с synthetic state проверяет рестарт своего нового
state, но не миграцию копии старого owner state: такую миграцию проверить отдельно
приватно без публикации копии. До этого live53+4 findings остаются открытыми.

