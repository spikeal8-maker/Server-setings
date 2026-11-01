# Школьная VM100 — прямое подключение ChatGPT к MCP

## Фактическая топология 01.11.2026

```text
ChatGPT (новый отдельный MCP-плагин)
    |
    | HTTPS + OAuth 2.1 + PKCE
    v
https://x.asa-lab.ru/mcp
    |
    v
Leticia / Traefik: действующий сертификат x.asa-lab.ru
    |
    v
FRP school VM100 (asa-frpc): localPort=80
    |
    v
1Panel VM100 → OpenResty → пути /mcp, /.idp/, /.auth/, /.well-known/oauth-*, /.well-known/jwks.json
    |
    v
127.0.0.1:39450 — независимый OAuth-шлюз VM100
    |
    v
127.0.0.1:39443/mcp — официальный mcp-1panel, full, 11 инструментов
```

**Независимость:** На VM100 отдельные секреты, systemd-служба, состояние OAuth, HTTP backend. Тестовый сервер `asa-test` и D2-R2 не участвуют в рабочем маршруте. Публичные порты backend `39443/39450` закрыты; HTTPS TLS заканчивается на Leticia. Имя `school-mcp.asa-lab.ru` из тестовой 1Panel **не относится** к VM100. Основной сайт `x.asa-lab.ru` сохранил proxy `/` → `127.0.0.1:4610`.

## Проверенные факты

- Через D2-R2, независимо от LAN VM100: `https://x.asa-lab.ru/.well-known/oauth-protected-resource/mcp` вернул HTTP 200 с точным `resource=https://x.asa-lab.ru/mcp`. Authorization-server metadata тоже HTTP 200, issuer `https://x.asa-lab.ru/`, PKCE S256, DCR и refresh grant объявлены.
- Публичный `https://x.asa-lab.ru/mcp` **без токена** дал HTTP 401 и `Www-Authenticate: Bearer resource_metadata="https://x.asa-lab.ru/.well-known/oauth-protected-resource/mcp"`.
- Публичный `https://x.asa-lab.ru/health/ready` остался 200/TLS verify0/schema286. Пять новых proxy locations созданы **штатным API 1Panel**. При первой проверке заголовка возникла ошибка регистра, штатный rollback отработал; ошибка исправлена, повторная публикация пяти paths завершилась PASS. Код `scripts/publish-school-mcp-via-1panel.py`.
- Локальный `server-settings-1panel-mcp.service` переведён в `full`: авторизованный `tools/list` выдаёт **11 инструментов 1Panel**, а не шестёрку readonly; локальный запрос без токена HTTP401. Исторический `readonly` конфиг сохранён root-only.
- Шлюз собран из exact upstream `c8868adee557e793b73c7e8a3b3d37e080854c20`, с существующим ASA RFC9728 патчем, два пакета Go-тестов PASS; двоичный SHA256 `114b57ca5cddf7206a6bd935bf1d8c342f10aab2ff6a0d76dad0840012178d15`. Локальный OAuth gateway отдельно проверен и запущен через systemd.
- **Граница:** подключение нового плагина через интерфейс ChatGPT и вызовы инструментов самим ChatGPT ещё **не подтверждены**. Не объявлять подключение READY до пользовательского OAuth и tool-call. И ещё 11 официальных 1Panel инструментов — не 23 инструмента тестового стенда: root shell/ASA operations backend требует отдельной адаптации к root-owned VM100.

## Единственное действие владельца в ChatGPT

1. Откройте **ChatGPT → Plugins** (в веб-интерфейсе).
2. Нажмите **+ → Add custom MCP server** (или «Добавить собственный MCP-сервер»). Если пункта нет, проверьте, разрешено ли создание пользовательских MCP-подключений в вашем тарифе или рабочем пространстве. Для модифицирующих инструментов возможны ограничения.
3. Название: **ASA School VM100 — 1Panel**. Endpoint: **https://x.asa-lab.ru/mcp**. Аутентификация: **OAuth**. Никаких Bearer-токенов, Client Secret или паролей в поле URL.
4. Прочитайте предупреждение безопасности, создайте подключение и пройдите OAuth. При открытии отдельной страницы шлюза VM100 пароль владельца берите **только из терминала самой VM100**:
   `sudo cat /var/lib/server-settings/asa-school-mcp-owner-password`
   Введите этот пароль в форму HTTPS `https://x.asa-lab.ru/.auth/login`; никуда больше не копируйте его, не вставляйте в этот чат, GitHub/Google Sheets и не включайте скриншот с паролем.
5. После успешного OAuth убедитесь, что ChatGPT обнаруживает 11 инструментов; первый проверочный запрос через **новый** плагин — `list_websites` или `get_system_info`. Изменения сайта/SSL тестировать только после подтверждения чтения и отдельно разрешённой безопасной операции. Не отключайте тестовый плагин до приёмки нового.

Согласно актуальной справке OpenAI, на ChatGPT web создание пользовательского MCP находится в Plugins → «+» → Add custom MCP server. Полные MCP write capabilities могут зависеть от плана/прав workspace: https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt.

## Как проверить и восстановить без тестового сервера

В терминале VM100, без вывода секретов:

```bash
sudo systemctl status server-settings-1panel-mcp --no-pager
sudo systemctl status asa-school-mcp-oauth-gateway.service --no-pager
sudo asa-school-connect status
```

В браузере без авторизации: `https://x.asa-lab.ru/.well-known/oauth-protected-resource/mcp` должно отдавать JSON с `resource`, а `https://x.asa-lab.ru/mcp` должно требовать авторизацию. Если `/mcp` отдаёт 200 без токена — **аварийно отключить публикацию MCP** через 1Panel; нельзя считать шлюз безопасным.

Если владелец потерял пароль, не удалять вручную состояние OAuth или credentials. Требуется отдельная контролируемая ротация с отзывом старого доступа и проверкой подключения ChatGPT; исходный пароль никогда не хранить в таблице.

## Влияние будущей смены домена

При переходе на другое имя `x.asa-lab.ru` или при переносе ASA Lab на `asa-lab.ru` нужно **дополнительно** заменить external URL OAuth-шлюза и токены клиентов с их привязкой к `aud=<новый-домен>/mcp`. Проверить issuer, metadata, DCR/PKCE, новый TLS и завершить повторное подключение ChatGPT к новому URL. Менять только DNS недостаточно: токены, выданные для прежнего `resource`, не должны считаться действительными для нового. Используйте `docs/30-SCHOOL-DOMAIN-MIGRATION-RUNBOOK.md` с отдельным rollback, сохраните прежний публичный сайт до приёмки нового.

## Прямой администратор ASA/Proxmox

Эталон теста описан в `docs/27-MCP-HOST-ADMIN-AND-CLOUD.md`. На VM100 сейчас `/srv/asa-lab` root-owned и `/etc/server-settings/asa-operations.json` отсутствует. Прямой перенос тестового `asa-operations` с непривилегированным владельцем не проходит проверку UID и опасен массовой сменой владельцев. Нужна отдельная адаптация именно в `Server-setings`, без изменения владельцев боевых PostgreSQL/MinIO и без публичного root-сокета.
