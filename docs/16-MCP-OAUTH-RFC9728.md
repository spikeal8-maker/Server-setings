# OAuth MCP через путь /mcp — проверенный патч для тестового сервера

**Статус: исправление разработано отдельно от работающей Ubuntu; до точного CI и интеграционной проверки НЕ ПРИМЕНЯТЬ.**

Почему: `sigbit/mcp-auth-proxy v2.10.2` сообщает `resource=https://94.29.44.211/`, хотя ChatGPT подключается к `https://94.29.44.211/mcp`. Запрос `/.well-known/oauth-protected-resource/mcp` получает 401 вместо документа metadata. Это соответствует открытой проблеме upstream https://github.com/sigbit/mcp-auth-proxy/issues/177 и RFC 9728; явная причинность ошибки ChatGPT пока не подтверждена логами OpenAI.

Данный patch pipeline клонирует фиксированный upstream commit `c8868adee557e793b73c7e8a3b3d37e080854c20` (tag v2.10.2) и точечно меняет четыре файла, без хранения полной копии стороннего проекта. Copyright/MIT лицензия остаются в исходниках образа (https://github.com/sigbit/mcp-auth-proxy/blob/v2.10.2/LICENSE).

## Контракт патча

- Endpoint `/.well-known/oauth-protected-resource/mcp` и root discovery возвращают `resource=https://94.29.44.211/mcp`; `authorization_servers` ссылается на издателя `https://94.29.44.211/`.
- Bearer/JWT валидируется по aud `.../mcp`; root aud токен более не принимается.
- Регистрация OAuth-клиента, authorize grant и access token выдают `/mcp` audience, при этом issuer остаётся корнем.
- Если OAuth запрос содержит `resource` отличающийся от `/mcp`, возвращается HTTP 400 `invalid_target`. Запрос без resource допускается для совместимости со старыми клиентами.
- Go-тесты доказывают RFC 9728 discovery, отказ root aud, разрешение /mcp aud, отказ подменённого resource, реальные code/token сценарии.
- Docker образ выпускается отдельно после CI и ревью. **Не переключать работающий MCP Compose до проверки, сохранения конфигурации и плана отката.**

## Процедура исполнения после CI

1. Проверить CI точного SHA с `go test ./pkg/proxy ./pkg/idp` и сборкой Docker. Проверить состав опубликованного OCI образа по SHA256/digest.
2. В 1Panel → Контейнеры → Composes открыть **только** `asa-1panel-mcp`; сохранить исходные YAML и .env, не выводя секреты в Issue.
3. Обновить только тег/digest образа шлюза (не касаться `asa-lab-dev`, `openresty` и `94.29.44.211` root proxy).
4. Вновь проверить `/.well-known/oauth-protected-resource/mcp` HTTP 200 с правильным resource и HTTPS, 401+WWW-Authenticate на /mcp без токена, OAuth authorization code + PKCE token exchange, вызов `tools/list` после входа.
5. Повторить создание ChatGPT custom MCP plugin и убедиться, что `list_websites` из ChatGPT отвечает без D2-R2. До этого статус **NOT READY**.
6. При ошибке откатить только шлюз на предыдущий digest и проверить ASA Lab `/health/ready` HTTP 200.

Исходная задача: https://github.com/spikeal8-maker/Server-setings/issues/9. Не путать кодовый патч и полностью испытанный публичный сервис.
