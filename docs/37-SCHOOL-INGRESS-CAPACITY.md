# Школьный ingress: ограничение общей нагрузки

Связанный programme: asa-lab#909. Это переносимое исправление конфигурации входа; применение и приёмка100 пользователей являются отдельными действиями.

## Подтверждённая причина

На Leticia Traefikv3.6 читает `/data/coolify/proxy/dynamic/asa-school-stage-routes.yaml`. Существующий `asa-stage-rate` допускает600 запросов/минуту (10/с), burst300. Этот middleware одновременно подключён к HTTPS роутерам `x.asa-lab.ru` и `test2.asa-lab.ru`. Явного sourceCriterion нет: общий школьный NAT расходует один IP-бюджет. Пауза5с между HTTP-действиями50 учеников приближает поток к10/с и потому обходила исходное ограничение; это не доказательство достаточной ёмкости для100.

Исходный публичный файл SHA256: `02f516ba49519483b310d36b8610e8d4666ba3f635935b11189991b7a2ad9ff4`, owner/group1000, mode0640. Доступ проверен существующим pinned SSH admins@Leticia:2221 и sudo. Локальный исходный deploy-script был только в operator SchoolSSH, не в GitHub main или дереве школьного PR27. Новый инструмент фиксирует повторяемую коррекцию в Git.

Школьный OpenResty также загружает Lua WAF. Его site CC profile200/10с существует, но master waf.state=off; WAF attack tables пусты. Эти конфигурационные данные сами по себе не доказывают WAF причиной429; выключать WAF или менять школьный vhost не требуется.

Отдельная причина AI504: `frps --help` подтверждает default vhost response-header timeout60с; серверная конфигурация не задаёт override. ASA SSE вызывает writeHead, но не flushHeaders и не передаёт начальные/периодические heartbeat до первого токена. Исправление SSE должно быть отдельным срезом приложения, а не глобальным повышением всех timeout.

## Предлагаемый ограниченный профиль100

`scripts/school-ingress-capacity.py` создаёт отдельный middleware только для выбранного точного TLS Host:6000 запросов/минуту (100/с), burst2000. Числа доступны как ограниченные параметры `--average`600..12000 и `--burst`300..5000. Это исходный профиль для измерения, а не заявление PASS100. Персональные quotas, login/admission и authentication middleware приложения сохраняются; sourceCriterion существующего rateLimit сохраняется. Не следует добавлять доверие к произвольному X-Forwarded-For или cookie/header как замену авторизованному principal.

Общий предшественник не меняется: test2 и другие роутеры сохраняют свои бюджеты. Сервисы, TLS, HTTP→HTTPS, остальные middleware и маршруты сохраняются семантически. Файл сериализуется как JSON-совместимый YAML, комментарии не сохраняются. Неизвестные auth/header цепочки вместо ожидаемого limiter, нестрогий Host, YAML aliases/duplicate keys и drift блокируют запись.

Зависимость указана явно: `scripts/requirements-ingress.txt` (PyYAML6.0.3, safe loader с отдельным YAML1.2 boolean resolver). Строки `off/on/yes/no` соседних конфигураций сохраняются строками; только `true/false` распознаются как boolean. Устанавливать в отдельный virtualenv из доверенного registry; отсутствие зависимости блокирует команду без изменения файла. CI использует тот же pinned requirements. На новом узле сначала штатно создаётся точный HTTPS route собственной установки; host и router передаются параметрами, домен/машинный IP в код не прошиты.

## План, применение, откат

Все команды сначала выполняются на копии конфигурации; работающий proxy не перезапускается, file provider должен быть настроен на watch.

```bash
python3 -m venv /opt/asa-ingress-venv
/opt/asa-ingress-venv/bin/pip install -r scripts/requirements-ingress.txt
/opt/asa-ingress-venv/bin/python scripts/school-ingress-capacity.py \
  --route-file /data/coolify/proxy/dynamic/asa-school-stage-routes.yaml \
  --host x.asa-lab.ru --router asa-stage-x-https
```

После независимого review плана добавить `--apply --expected-sha256 FRESH_SOURCE_SHA --backup-directory /PRIVATE/ingress-backups`. Перед заменой исходный SHA проверяется снова; exact bytes и квитанция сохраняются mode0600 вне каталога наблюдения, затем выполняется атомарная замена с сохранением uid/gid/mode. На POSIX fsync выполняется для файлов, каталога операции и цепочки вновь созданных каталогов до замены target. Симлинки, Windows reparse points, hardlinks и большой ввод не принимаются. Проверка отсутствия ссылок предшествует канонизации `..`; placement backup сравнивается по каноническим путям.

Публичный план содержит только проверенные числовые budgets, допустимую duration и признаки `hasRedis/hasSourceCriterion`. Redis credentials, endpoints и содержимое sourceCriterion сохраняются в настоящем YAML, но не выводятся. Ошибка YAML содержит только класс и line/column без исходной строки или exception context.

Применение не означает успешную runtime-приёмку (`liveVerification=NOT_RUN`). После него проверить TLS/public health, соседние x/test2/production hosts, появление нужного middleware в runtime, контролируемый burst100 учеников за одним NAT и сохранение authentication/admission ограничений. Для доказательства сохранения perimeter limiter допустим отдельный ограниченный synthetic limiter test; не выдавать отсутствие429 в малом тесте за отключение защиты.

Откат: та же команда с `--restore /PRIVATE/operation/receipt.json --apply --expected-sha256 CURRENT_APPLIED_SHA --backup-directory /PRIVATE/ingress-backups`. Проверяются hashes backup/current и точный target; новые несвязанные изменения файла нельзя затереть. Перед откатом текущий вариант также резервируется. Службы и shared Docker не перезапускаются.

## Статус доказательств

Read-only диагностика выполнена на действующих VM100 и Leticia. Unit-тесты проверяют scope, сохранение test2/auth/sourceCriterion/Redis, idempotence, отрицательные YAML-входы и отсутствие секретов в реальном CLI, YAML1.2 boolean semantics, exact-SHA apply, канонический backup placement, порядок durable backup и побайтовый rollback. До отдельного опубликованного/reviewed SHA и фактического применения статус этого исправления — candidate, deployment NOT_RUN, capacity100 NOT_RUN.
