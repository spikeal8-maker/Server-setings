# MCP: проверенный образ-кандидат, не подключённый ChatGPT

Дата: 20 октября 2026. Продолжение PR #12 и Issue #9, не новая установка 1Panel.

## Реализовано в ветке

- Сохранён исходный патч RFC 9728: resource и JWT audience `/mcp`, path-specific metadata, отклонение подмены resource.
- Подготовщик проверяет exact upstream `c8868adee557e793b73c7e8a3b3d37e080854c20` и чистое дерево до изменений; затем применяет существующий патч.
- 401-ответы без токена и с неверным токеном сами содержат WWW-Authenticate с адресом path-specific metadata. Адрес берётся из конфигурации, не из недоверенного Host запроса.
- DCR возвращает фактические RFC 7591 defaults вместо null grant_types/response_types. Public client по-прежнему не получает client_secret.
- Новые Go-регрессии проверяют GET/POST, плохую авторизацию, два hostname, открытые metadata и регистрацию OAuth-клиента.
- Отдельный workflow выполняет Python tests, все Go packages, race tests proxy/idp, сборку linux/amd64 и запуск бинарника с --help.
- Только после тестов образ сохраняется в архив с SHA256, исходниками, MIT-лицензией и manifest; отдельный job публикует кандидат в GHCR. Издатель получает packages:write, тестовый job — только contents:read. Ни один job не обращается к реальному серверу и не получает его ключей.

## Где результат

GitHub → Actions → «MCP candidate — tests, image and private release» → запуск точного коммита. Артефакт `asa-mcp-candidate-` содержит архив Docker, а `asa-mcp-published-digest-` — точный опубликованный OCI digest. Имена заканчиваются полным SHA коммита.

Новый пакет GHCR по умолчанию приватный. Не изменять видимость на публичную. Для импорта через интерфейс 1Panel можно использовать проверенный Docker-архив вместо настройки новых registry credentials. Успешная сборка/публикация НЕ означает развёртывание или прохождение пользовательского OAuth.

## Применение только через разрешённый административный сеанс

1. Сохранить локально предыдущий Compose, его закрытый .env, Docker image digest и конфигурацию обратного прокси. Секреты не переносить в GitHub/чат.
2. Сначала на отдельном изолированном preview пройти OAuth code + PKCE + token + MCP tools/list. Разрешённые инструменты — readonly. Независимый review обязателен до рабочего переключения.
3. В 1Panel изменять только `asa-1panel-mcp`, сохраняя `asa-1panel-mcp-gateway`, существующий mount официального `mcp-1panel`, state volume, настройки ключей и loopback 127.0.0.1:39443. Меняется образ шлюза на проверенный digest. Не изменять проекты `asa-lab-dev` и `openresty`.
4. Для конечного адреса `https://test.asa-lab.ru/mcp` внешний URL шлюза должен быть `https://test.asa-lab.ru` (issuer в metadata может иметь завершающий `/`; его точное значение должно совпадать с токеном). Resource/audience — строго `https://test.asa-lab.ru/mcp`.
5. В сайте `test.asa-lab.ru` через 1Panel добавить маршруты `/mcp`, `/.well-known/oauth-`, `/.well-known/jwks.json`, `/.idp/`, `/.auth/` к `http://127.0.0.1:39443`, сохраняя исходный URI и streaming. Не перехватывать весь `/.well-known/`: ACME должен остаться нетронутым. Сохранить корневой маршрут ASA Lab.
6. Старое статическое правило WWW-Authenticate не должно подставлять IP для доменного подключения или дублировать заголовок шлюза. Удалять/менять только это правило в MCP location через 1Panel после проверки кандидата; root ASA и ACME не менять.
7. Проверить metadata 200, resource `/mcp`, 401 и challenge без токена, отказ неверного audience, DCR, реальный пользовательский OAuth и три readonly-вызова из ChatGPT. Дополнительно ASA Lab health и вход/сохранение проекта. Отдельно проверить reload/reboot и сохранение OAuth state. Эти проверки ещё НЕ выполнены данным workflow.
8. При неуспехе вернуть предыдущий образ и внешнюю конфигурацию шлюза/его маршрутов, проверить ASA Lab; не удалять volumes и не переустанавливать базу.

## Подтверждённая граница 20.10.2026

Публичный IP: `/mcp` 401+challenge, root metadata 200 с неправильным resource `/`, path metadata 401. Домен: MCP 404, ASA Lab ready 200, release 314078ba, schema 213. SSH-вызов из текущего чата заблокирован защитой инструмента; Browser не получил контекст браузера. Обходов нет. До разрешённого доступа и реального применения — **NOT DEPLOYED**, до OAuth и вызовов из ChatGPT — **NOT CONNECTED**.
