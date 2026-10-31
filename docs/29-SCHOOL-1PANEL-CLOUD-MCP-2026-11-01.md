# Школьный Proxmox: передача управления в 1Panel, MCP, Яндекс Диск

**Статус: частичное внедрение, эксплуатационная приёмка не пройдена.** Актуализация 01.11.2026.


## Дополнение 01.11.2026 — исправление Yandex OAuth 410

- При первой авторизации VM100 токен был записан штатно (uid 1000, mode0600), но `cloud_storage.py status` показал `token_configured=true`, `connected=false`, `provider_error=CLOUD_PROVIDER_GONE`. Отдельная диагностическая проба `rclone lsjson yandex:` и `lsjson --stat yandex:ASA-Backups` дала HTTP 410 Gone; `rclone about yandex:` успешно. Это свидетельствует о недостаточной авторизации файлового API, а не о сбое Docker/контейнера.
- На D2-R2 исходный существующий `rclone-own.conf` имеет отдельный custom OAuth Client ID/Secret, и `lsjson --stat yandex:ASA-Backups` через него успешно работает. Прежний OAuth-токен D2-R2 не пересылался на VM100.
- Проверен отдельный временный процесс `rclone authorize yandex --auth-no-open-browser` с `RCLONE_YANDEX_CLIENT_ID/SECRET` из защищённой конфигурации в environment (не argv). Проверено: переход в OAuth Яндекса использует именно ожидаемый Client ID; временная проверочная авторизация остановлена без выдачи токена.
- Для одноразового восстановления создан `scripts/repair-school-yandex-oauth.py`: принимает новый OAuth и client settings **только stdin** по pinned SSH/QEMU, валидирует реальную ошибку 410 старого стандартного OAuth, права и owner файла, после замены проверяет `ASA-Backups`; при любой ошибке возвращает исходный конфиг. Прямое переписывание 1Panel / ASA баз, миграции, автокопии исключены.
- Операторское окно `ASA Lab - Yandex Disk VM100` на D2-R2 обновлено, передача нового токена только через pinned SSH и QEMU Guest Agent; GUI перезапущен, пользовательский новый OAuth ещё требуется. `204` unit-тестов `Server-setings` на VM100 PASS, включая регрессионные тесты восстановления; `py_compile` GUI и VM-helper PASS.
- **Статус на момент проверки:** VM100 OAuth токен присутствует, но `connected=false` (410); новый токен через пользовательский custom OAuth ещё не выдан. Считать Яндекс Диск подключённым только после успешного `cloud_storage.py status connected=true` и `lsjson --stat yandex:ASA-Backups` в VM100. Бэкапы и scheduler выключены.

## Дополнение 01.11.2026 — TLS, Tailscale и Яндекс OAuth

### Публичные сертификаты и 1Panel

- Публичный HTTPS для x.asa-lab.ru завершается на внешнем Leticia/Traefik. VM100 1Panel управляет внутренним HTTP-сайтом и не располагает внешним сертификатом, поэтому раздел «Сертификаты» VM100 пуст. Не копировать закрытый ключ с Leticia и не выпускать дублирующий ACME-сертификат без отдельного решения о TLS termination.
- Через D2-R2 проверены реальные TLS 1.3 / Let's Encrypt сертификаты: x.asa-lab.ru и test2.asa-lab.ru действуют до 2027-01-29T19:45Z; asa-lab.ru до 2027-01-18T13:00Z; test.asa-lab.ru до 2027-01-17T20:18Z; school-mcp.asa-lab.ru до 2027-01-27T07:45Z. Доказательство автоматического продления **внешнего Traefik** ещё не собрано.
- Старый 1Panel H-03 выдаёт отдельный IP-сертификат для 94.29.44.211, срок до 2026-11-06T06:01Z, статус autoRenew=true. По правилам Let's Encrypt сертификат для IP намеренно короткий (~160 часов); необходим контроль фактического продления.
- 1Panel «Приложения» и Docker «Контейнеры» — разные каталоги. OpenResty управляется 1Panel, asa-lab-school управляется существующим Compose: 6 healthy long-running и 2 успешно завершённых migration/minio-init (Exited 0). Отдельный asa-cloud-storage-rclone-1 healthy ещё не означает авторизацию Яндекс.

### Яндекс — отдельный владелец подтверждает OAuth на D2-R2

- На D2-R2 ранее настроенный rclone OWN конфиг (D:\ASA25\rclone-auth\rclone-own.conf) проверенно читает Yandex ASA-Backups. Прежний токен **не копировался** на VM100.
- Созданы GUI %LOCALAPPDATA%\ASA-Lab\SchoolSSH\connect_school_yandex_vm100.py и ярлык ASA Lab - Yandex Disk VM100 на D2-R2. Владелец нажимает «Авторизовать Яндекс Диск», в браузере подтверждает новый OAuth. Токен передаётся без чата/аргументов через pinned SSH D2-R2 → PVE:14626 → QEMU Agent VM100.
- Серверный scripts/store-school-yandex-oauth.py принимает только stdin, проверяет владельца/mode0600 выделенного пустого rclone.conf, отказывается перезаписать существующий OAuth. Затем GUI запускает штатный cloud_storage.py status для проверки ASA-Backups. До входа владельца VM100 token_configured=false и connected=false; backup_scheduler=false. Не включать автокопирование до подтверждения шифрования age и репетиции восстановления.

### Tailscale VM100 и MCP

- Текущая VM100 сообщает NeedsLogin, адрес и node ID отсутствуют; наличие в D2-R2 одноимённого peer с IP 100.124.247.24 не доказывает его принадлежность VM100.
- Только внутри VM100 перезапущен tailscaled без очистки persistent state. Новый tailscale up с accept-dns=false/accept-routes=false/netfilter-mode=off и hostname=asa-school-01 запущен через QEMU Guest Agent асинхронно, guest task PID 220300; операция ожидала авторизации. Свежая ссылка предоставлена владельцу **только в чате**, не записывается в GitHub или Sheets. Окончательная приёмка: VM100 Running, назначен IP, удалённый tailscale ping проходит.
- Локальный MCP VM100 слушает 127.0.0.1:39443/mcp (readonly). В этой беседе уже доступен защищённый операторский путь через D2-R2 → pinned SSH Proxmox → QEMU Agent. Прямой облачный ChatGPT-коннектор к VM100 требует отдельного HTTPS/OAuth шлюза, не готов. Не открывать MCP в WAN и не переиспользовать public school-mcp.asa-lab.ru, принадлежащий другому серверу.

## Изменения по живому серверу VM100 — 01.11.2026

- Создан и проверен online Proxmox ZFS snapshot `before-1panel-openresty-20261101` с QEMU Guest Agent freeze/thaw. Это локальная точка отката, не независимая копия.
- Исправлена ошибка в `scripts/panelctl.py`: в `/apps/install` для OpenResty раньше не передавался обязательный параметр `WEBSITE_DIR`, поэтому 1Panel записывал `InstallErr` (`invalid spec: :/www`). Ошибка на VM100 устранена штатным API `/apps/installed/params/update`, без ручной записи в SQLite. Теперь OpenResty `1.27.1.2-5-1-focal` и контейнер `1Panel-openresty-Pozw` имеют `Running`.
- Сайт `x.asa-lab.ru` (`asa-lab-x`) создан через официальный API 1Panel, `proxy -> http://127.0.0.1:4610`. `websites/search` возвращает `Running`, локальный запрос `Host: x.asa-lab.ru` к OpenResty:80 `/health/ready` вернул 200. В 1Panel VM100 нет локального сертификата; внешний TLS/FRP на Leticia пока не переключён на этот OpenResty. Основной `asa-lab.ru` не менялся.
- Официальный локальный `mcp-1panel` активирован с `readonly` и `DynamicUser`. Исправлено второе несоответствие: бинарник `mcp-1panel`, собранный при `umask 077`, имел права `0700` и давал systemd `203/EXEC`; выставлены `0755` **только для исполняемого файла**. `/etc/server-settings/mcp.env` остаётся root-only `0600`. Служба `server-settings-1panel-mcp` — active; `127.0.0.1:39443/mcp` без Bearer возвращает 401, с локальным сохранённым Bearer MCP `initialize` возвращает 200. Публичный OAuth/HTTPS MCP gateway для ChatGPT не создавался; не считать старое тестовое MCP подключением новой VM100.
- `tailscaled` активен, но `tailscale status --json` **внутри VM100** возвращает `BackendState=NeedsLogin`, `Self.Online=false`, `TailscaleIPs=null`. D2-R2 отображает peer с таким же `HostName=asa-school-01` и адресом `100.124.247.24`, однако `tailscale ping` сообщает `unknown peer`; идентичность/живой доступ к VM100 по этому IP не подтверждены. Не публиковать одноразовые AuthURL в GitHub, документации или Sheets. VM100 `eth0` к школьной LAN остаётся `link_down=1`.
- После исправлений `197` unit-тестов `Server-setings` прошли на VM100; `bash -n` для MCP и `python3 -m py_compile` для `panelctl.py` PASS. Статусы `ASA Lab /health/ready`, MCP и proxy PASS; данные PostgreSQL/MinIO и внешний `asa-lab.ru` не менялись.
- Ветка PR #27 остаётся **Draft** до независимого review, CI и эксплуатационной приёмки: browser login, save/reopen, подтверждённая сетевая политика, offsite backup и тест восстановления. Этот документ уточняет `README.md`, `docs/04-ПУБЛИКАЦИЯ-САЙТОВ.md`, `docs/05-API-И-MCP.md` и `docs/27-MCP-HOST-ADMIN-AND-CLOUD.md` для новых школьных VM; не переносит настройки старого тестового Ubuntu `admins-MS-7D22` без проверки.

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
