# Школьный Proxmox: передача управления в 1Panel, MCP, Яндекс Диск

**Статус: частичное внедрение, эксплуатационная приёмка не пройдена.** Актуализация 01.11.2026. Этот документ уточняет `README.md`, `docs/04-ПУБЛИКАЦИЯ-САЙТОВ.md`, `docs/05-API-И-MCP.md` и `docs/27-MCP-HOST-ADMIN-AND-CLOUD.md` для новых школьных VM; не переносит настройки старого тестового Ubuntu `admins-MS-7D22` без проверки.

## Фактическое состояние — проверено через Proxmox/QEMU Guest Agent

| Узел | ASA Lab | 1Panel | Яндекс Диск |
|---|---|---|---|
| VM100 `asa-school-01` | `asa-lab-school`, release `de1efb9b45a0ee10233cab53a066cb0c8c4f121b`, схема 286, 6 healthy | 1Panel OSS 2.3.2, core/agent active, OpenResty не установлен, app_installs=0, websites=0, API Disabled, ApiKey отсутствует | `asa-cloud-storage-rclone-1` healthy, образ закреплён, токен **не авторизован**, расписание выключено |
| VM101 `asa-school-test-01` | `asa-lab-test2`, тот же release, отдельная БД, 6 healthy | Не установлен; вручную не регистрировать отдельную БД в панели VM100 | `asa-cloud-storage-rclone-1` healthy, тот же образ, токен **не авторизован**, расписание выключено |

Проверен Git blob `32fd5e811de3547ff36a2a93cc952f78ee25f7fd` для `scripts/cloud_storage.py`: идентичен `main` и школьной ветке. Docker rclone: UID1000, rootfs read-only, cap_drop ALL, собственные config/data, без публичных портов и томов рабочей БД. Это **не успешное соединение с Яндекс ID**, пока OAuth не завершён.

Публичные `https://x.asa-lab.ru` (VM100) и `https://test2.asa-lab.ru` (VM101) идут через Leticia/Traefik → авторизованный FRP → внутренний Web, HTTPS/health 200; существующий `asa-lab.ru` не переключали. Это **не 1Panel-managed website**, пока OpenResty отсутствует. Школьные NIC VM отключены; временный QEMU NAT не является окончательной LAN.

## Порядок доведения до управляемой установки, без обхода 1Panel

1. Владелец в VM100 1Panel через защищённый D2-R2 туннель открывает **Настройки → API**, включает API, создаёт отдельный `asa-lab-private-mcp` с allowlist **127.0.0.1**, сроком подписи **5 минут**, без «любой IP». Секрет сохраняется на VM100 `/var/lib/server-settings/1panel-api.key` root0600 через `scripts/store-panel-key.py`, проверяется `scripts/panelctl.py inspect`. Никогда не редактировать напрямую `/opt/1panel/db/`.
2. Через официальный 1Panel API `scripts/panelctl.py install-openresty --openresty-version ... --apply` выбрать реально опубликованную для Ubuntu 24.04 версию; до записи проверка 80/443 и app-store. Создать из 1Panel proxy-сайт `x.asa-lab.ru` с upstream `http://127.0.0.1:4610`. Заранее сохранить проверенный путь возврата к текущему FRP; только после HTTP/Host/proxy health переключать FRP на внутренний OpenResty :80 и проверять внешний TLS/CSRF. Действующий Leticia Traefik управляет **публичным TLS**, VM100 1Panel — **внутренним proxy-сайтом**. Не менять `asa-lab.ru`.
3. Официальный локальный `scripts/setup-mcp.sh --readonly --apply` только после проверенного panel-api.key; bind 127.0.0.1:39443, unauth 401, без публичного прокси. Отдельный OAuth/HTTPS gateway для ChatGPT и `asa-operations` привязать к **реальной** VM100 installation UUID, project `asa-lab-school`, approved scopes, аудит и отдельное решение о root `asa_admin_execute`. Подключённый ранее MCP от другого Ubuntu-хоста **не является** MCP VM100.
4. Яндекс Диск: использовать только существующий `scripts/cloud_storage.py`, не создавать новую реализацию. Отдельные OAuth-доступы и ограниченные права на папки, `asa-lab` → `ASA-Backups`, `asa-lab-test2` → `Server-Backups/asa-lab-test2`. После подтверждения OAuth проверить encrypted age upload/download с SHA256 **без** изменения PostgreSQL/MinIO.
5. Ночные копии — **только в окне низкой нагрузки по Europe/Moscow**, без «каждые 6 часов» и без догоняющих запусков днём (`Persistent=false`); ограничить скорость/IO и разнести VM100 и VM101. Точное окно и retention до выполнения согласовать, в репозитории пока **нет включённого scheduler**. Нужны внешний backup и независимое восстановление на чистом test/staging узле до production.
6. Зафиксировать реальные 1Panel сайты, backup и MCP в `snapshots/`, `docs/08-ПРОВЕРКИ-И-ОГРАНИЧЕНИЯ.md`, провести owner browser/save/reopen, reboot и security acceptance; лишь затем согласованное слияние ветки в main.

### Жёсткие ограничения

- Не перезапускать без необходимости работающие PostgreSQL/MinIO и контейнеры ASA, не переписывать `.env` ради регистрации сайта, не делать `docker compose down -v`.
- Не включать неаутентифицированный MCP в интернет; API-ключ 1Panel сам по себе привилегированный.
- Не отождествлять список 1Panel «Приложения» с запущенными внешними Compose-контейнерами: последние видны в «Контейнеры», но штатным менеджером приложения ASA Lab остаётся `tools/asa_manager.py`.
- Ни один отсутствующий API/OAuth/backup/restore/browser gate не считать успешным из-за одного HTTP 200.
