# Изолированное наблюдение существующих Ollama/ComfyUI

Это **source-only** инструмент для отдельно разрешённой установки. Host installation,
real provider requests, systemd и user-reader integration этим PR не проверены.
Он не запускает генерацию, не меняет модели/GPU, приложение, Compose, сеть или порты.

`scripts/provider_health_exporter.py` выполняется только Linux root, через Python
`-I -B`. До imports он открывает каждый ancestor без symlink, требует root ownership
без group/other write, single-link regular files и проверяет **raw length + SHA256**
трёх Python-файлов из immutable app `2a28cffe7a20302f9b7231ef823bdc05f3fe43d0`:

| Относительно выбранного tools bundle | Bytes | Raw Git SHA256 |
| --- | ---: | --- |
| deployment/__init__.py | 66 | b894d56afdcda00c7f34bab54c979d957581cadb120a414f207397850fd70a1b |
| deployment/paths.py | 2477 | 517981b0847c1fb271eb407d2084ef4db08cc87f0a03bd0a8c84d8a73f6a434b |
| deployment/operational.py | 26444 | c538817ec94cc16df51cec1cc4054789a4f0c873595eaf6263987a2ca2909ffa |

Код не vendored, USER checkout не импортируется и `sys.path` не подменяется.
Проверенные байты исполняются в отдельном in-memory `deployment` package; collision
с уже загруженным package даёт STOP. `operational.fetch_json`, `origin` и
`provider_sample` используются без monkeypatch в production. Разрешены только
Ollama `GET /api/ps`, `GET /api/tags` и ComfyUI `GET /system_stats`: исходные
3-second HTTP/body deadline, 256KiB response cap и numeric private origin guard
сохранены. Нет DNS, redirects, credentials, proxy discovery или retries.

Имена моделей/устройств, URLs, response bodies и exception text не публикуются.
Выход содержит provider ID, UTC-время, closed availability/failure enum, durations
и bounded integer aggregates. `reachable` означает доступность этих GET, а не
наличие пригодной модели, успешную генерацию или качество результата.

## Конкретная ручная установка после review

Автоматического installer нет. До действий оператор должен утвердить один host,
его существующие numeric LAN/loopback origins, reader GID и отдельный output path.
`templates/provider-monitor/config.example.json` — пример, **не реальные настройки**;
loopback origins нельзя слепо переносить на другой узел. Существующие root-trusted
17 CLI files можно использовать как bundle: проверяются только три pinned файла.
Переносимый example bundleRoot — `/opt/asa-provider-monitor/app-source/tools`:
оператор отдельно размещает там только `deployment/__init__.py`, `paths.py` и
`operational.py` из exact Git raw blobs app2a; lengths66/2477/26444 и SHA256 указаны
в таблице выше. Все ancestors/files root-controlled; обычный mutable USER checkout
или guessed/normalized bytes не подходят. Example не создаёт эти файлы сам.
Существующий H03 root-trusted CLI bundle
`/var/lib/asa-stabilization-cli-909-2a28cffe7a20/tools` может быть явно выбран
оператором в реальном config без повторного копирования. Этот incident path не
является переносимым default или production-code dependency.
Настоящий installed USER checkout не подходит, даже при том же Git SHA.

Разместить exact reviewed exporter в `/opt/asa-provider-monitor/`, root-owned code
без group/other write; config — `/etc/asa-provider-monitor/config.json`, root0600.
Все ancestors должны принадлежать root без delegated write. Dedicated output
`/var/lib/asa-provider-monitor` заранее создаётся root:`readerGid`, mode0750.
Это persistent private state с отдельным `health.log`; config outputDirectory и
unit ReadWritePaths должны указывать один approved path. Проверять реальных
ancestors на каждом host, а не предполагать их безопасность по имени `/var/lib`.
Не использовать business/forensic/USER directories; чужой entry в output даёт STOP.
Config требует ровно два provider IDs, явные origins, bundle/output paths и GID;
секретов и implicit defaults нет. Проверить целевой GID и возможность чтения его
существующим collector owner; никаких новых пользователей или mode conversion.

До установки units отдельно проверить source hashes, PLAN и синтаксис units:

```sh
sudo python3 -I -B /opt/asa-provider-monitor/provider_health_exporter.py --config /etc/asa-provider-monitor/config.json
systemd-analyze verify /APPROVED/asa-provider-monitor.service /APPROVED/asa-provider-monitor.timer
```

PLAN проверяет пути/bytes/config/output, **не делает GET, mkdir, chmod, lock или writes**.
Root-owned service/timer templates имеют fixed `-I -B`, oneshot, 60s timer,
30s systemd deadline, capability bounding и read-only filesystem с одним writable
output directory. Если operator paths отличаются, нужен review соответствующего
unit projection; не ослаблять hardening для обхода отказа.

Перед копированием units проверить занятость обоих имён. Неизвестный existing unit,
drop-in или отличающиеся bytes — STOP, без overwrite. Повторная установка допустима
как проверка тех же approved bytes/config/modes; generic overwrite/update не реализован.
Только после этого отдельный authorized operator может установить templates,
reload systemd и включить **только этот** timer. Этот документ сам действий не выполняет.
Первый реальный root staging test/GET и проверка actual hardening обязательны;
Windows focused tests их не заменяют. Нельзя перезапускать app/GPU/другие services.

## Receipt, журнал и отдельный availability audit

`sample --apply --config ...` использует existing dedicated directory, exclusive
nonblocking lock и сохраняет root:`readerGid` `health.log`0640. Rotation ограничена
пятью файлами по1MiB. Foreign names, links, modes, oversize/partial/foreign JSON
отклоняются; rotated artifacts никогда не считаются общим log-directory cleanup.
Append/fsync предшествует atomic latest0600/fsync-directory. При сбое partial file
не очищается автоматически: STOP с сохранением evidence. `check` также требует
совпадения latest с последней строкой журнала и поэтому не принимает незавершённую
публикацию за свежий успех.

Receipt имеет закрытый event envelope для существующего file adapter app2a:
`time` строго равен `checkedAt`, `kind` строго `provider_health`, `level` —
`error`, если хотя бы один validated provider unavailable, иначе `info`.
Validator отклоняет missing/forged/mismatched поля. Тот же полный JSON с newline
публикуется в log/latest; `error` receipt остаётся корректным свидетельством
реальной неготовности, и audit возвращает `PROVIDER_UNAVAILABLE`.
Новых imports collector в production нет. Test-only чтение exact Git raw
`tools/collect_logs.py` app2a (138984 bytes,
SHA256 `8e728afecab47a89ac73261fc588faf7be172f6ff32b77db16138d7d079a00f2`)
через `ASA_PROVIDER_TEST_SOURCE` извлекает только перечисленные pure functions/constants
AST, без module imports/top-level I/O. Реальные `file_timestamp` и `normalize`
проверяют success и отдельный failure каждого provider: event time вместо mtime,
info/error соответственно. Legacy envelope показывает прежний mtime/info fallback.
Это не меняет consumer taxonomy и не квалифицирует API required-provider policy.

```sh
sudo python3 -I -B /opt/asa-provider-monitor/provider_health_exporter.py sample --config /etc/asa-provider-monitor/config.json --apply
sudo python3 -I -B /opt/asa-provider-monitor/provider_health_exporter.py check --config /etc/asa-provider-monitor/config.json
```

Первый CLI возвращает nonzero при unavailable provider, сохраняя closed failure
receipt; следующий timer не является генерационным retry. `check` не делает GET и
не создаёт файлов: shared lock, root/mode/link checks, точная typed schema,
config fingerprint, exact two IDs, availability и все реальные timestamps в
0..120s. Empty, stale, future, failed, changed-config или divergent receipt — FAIL.
Убийство процесса/ошибка storage не превращается в успех; audit может подтверждать
только последнюю сохранённую проверку в данном freshness window.

Для отдельно разрешённого tailing используйте source ID **`provider-monitor`** и
файл `health.log`. Его tailer state `ok` доказывает только чтение файла.
Он **не** означает `provider:ollama`/`provider:comfyui` availability, не заполняет
app required-provider policy и не исправляет другие missing rules/gaps. Нужны
одновременно свежий независимый `check` и фактическое чтение collector. API required
coverage, native writer/integrity, journal/history/lifecycle gaps проверяются отдельно.

Focused gate, без Docker/HTTP/install:

```sh
ASA_PROVIDER_TEST_SOURCE=/EXACT/CLEAN/2a python3 -B -m unittest discover -s tests -p test_provider_health_exporter.py -v
```

Новая CI matrix получает существующий **public** app repo обычным checkout точного
2a, без новых secrets; raw pins проверяются до тестового module execution. Linux FD
tests используют только temp files с явно simulated root metadata; link/CAS/flock/
rotation operations реальные. Native negative rejects an actual untrusted temp
ancestor. Реальная systemd/provider/reader qualification остаётся отдельной.

Первый root staging exact `5bea825d3829fe5ec9891a71749a845c5f1cbab8`:
22 run, 21 PASS, 1 FAIL, 0 skip (operation
`1c5287c6665943fb921082dcc5db20b2`). Единственный FAIL — PLAN fixture:
root umask `0077` сделал test output `0700` вместо обязательного `0750`.
Однократный capture `3f9dd8e74e9e4a02902df83588a38e61` подтвердил закрытый
`OUTPUT_DIRECTORY_MODE`, без реальных provider requests.
Исправлена только подготовка fixture: явный `chmod(0750)` после `mkdir`.
Production guards/bytes не изменены. Targeted in-memory replay ровно этой
поправки на тех же проверенных staged bytes при umask `0077`:
1 PASS, CLI PLAN/exit 0, operation `cac90d0295324894af7cae85f24027ef`.
Staged source, app/services не менялись; новый полный root suite не заявлен.

Отдельный installation preflight `0ba9fea6369040009a0f5b85fcb5e42d`
остановился на parent trust до любой записи; diagnosis
`7079c7c1bf294db0a0dc8472bed5226e` подтвердил `/var/log` root:102 mode0775
и `/var/lib` root:root mode0755. Все шесть installation targets отсутствовали,
activation не выполнялась. Default example/template/docs перенесены в
`/var/lib/asa-provider-monitor`; права `/var/log` и строгие production guards
не менялись. Новый path PLAN, real probes и service activation этим source patch
не выполнены; требуются отдельные review и operator preflight.
