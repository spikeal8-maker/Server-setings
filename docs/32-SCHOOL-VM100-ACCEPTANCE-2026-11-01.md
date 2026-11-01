# ASA Lab School VM100 — реальная приёмка на 01.11.2026

Источник истины: `Сетьевая конфигурация` → вкладка **13 Приёмка VM100**,
https://docs.google.com/spreadsheets/d/12VHlK1sko6g8T_xn_13jjQd_USFkW7tlV73yMRhAjwE/edit#gid=512.
Репозиторий: PR #27 (Draft). Не путать завершённые unit-тесты с успешным production cutover.

## Проверено на действующем школьном VM100

- Proxmox VM100, Ubuntu и 1Panel/agent активны, `onboot=1`; повторный reboot с полной эксплуатационной приёмкой пока не проведён.
- `https://x.asa-lab.ru/health/ready` HTTP 200, TLS доверенный, приложение ASA Lab schema286; публичный тракт Leticia/Traefik → FRP VM100 → OpenResty 1Panel :80 → ASA Lab :4610.
- `asa-cloud-storage`: собственный Yandex OAuth client/token/refresh в приватном файле VM100; `status.connected=true` подтверждает **чтение** `ASA-Backups`. Статус `connected` **не подтверждает права на upload**.
- `tailscaled`: `Running`, `Self.Online=true`, IP `100.124.247.24`, без Health ошибок; ICMP и TSMP между D2-R2 и VM100 проходят через DERP. Ключ узла истекает 30.04.2027 10:23 МСК, а одноразовый регистрационный ключ — 30.01.2027 (он больше не нужен работающему узлу).
- VM100 официальный `mcp-1panel`: localhost `127.0.0.1:39443/mcp`, `full`, 11 инструментов, authenticated `tools/list=200`, unauth=401; **не** является root/ASA arbitrary shell.
- Независимый VM100 OAuth MCP gateway на localhost `127.0.0.1:39450`, опубликован `https://x.asa-lab.ru/mcp` через 5 штатных 1Panel locations; внешние OAuth resource/server metadata =200, без авторизации MCP=401 с WWW-Authenticate. Пароль владельца — закрытый файл **на VM100**, не в GitHub/документации. `school-mcp.asa-lab.ru` прежнего теста — другой ресурс.

## Закрыта только локальная стадия шифрования конфигурации

- Из существующего owner-held USB age identity получен **только публичный recipient**; приватный key никогда не копировали/не передавали на VM100. Публичный recipient хранится в `/etc/server-settings/age-recipients.txt`, root0600; `age` Ubuntu 1.1.1 установлен.
- `bash scripts/backup-settings.sh --apply` сформировал **только конфигурационный** архив
  `/var/backups/server-settings/server-settings-20261101T121900Z.tar.age`: **7 292 856 байт**, root0600, `sha256sum --check` PASS. В него **не** включены данные PostgreSQL, MinIO, образы Docker и проекты.
- Для передачи в `Server-Backups/server-settings` добавлен повторяемый `scripts/school-settings-cloud-roundtrip.py` (immutable upload/download SHA256 через существующий `cloud_storage.py`). Открыт P0: **Yandex upload возвращает `CLOUD_AUTHORIZATION` (401/403)**. Дважды проверено, удалённая копия и проверка download отсутствуют. Причина не доказана: проверить права `cloud_api:disk.write` собственного OAuth-приложения и write-права папки. Не менять рабочий read-OAuth или облачный маршрут без подтверждения.
- Backup scheduler **не включён**, потому что пользовательский критерий — **только ночные** проверенные резервные копии. До расписания нужны реальный encrypted upload/download, отдельная backup PostgreSQL+MinIO, изолированный restore и аварийный отчёт.

## Открытые P0

1. **Новый плагин ChatGPT → VM100.** Опубликованный HTTPS endpoint не равен подключению. Владелец добавляет в ChatGPT Plugins собственный MCP с `https://x.asa-lab.ru/mcp`, выполняет OAuth через VM100 и принимает реальный `list_websites`/другое read действие из ChatGPT. Документация — `docs/31-SCHOOL-MCP-CHATGPT-CONNECTION.md`.
2. **Полные ASA/OS root полномочия.** На тестовом эталоне есть `asa-operations`, `asa_admin_execute` и root-аудит, но на VM100 `/srv/asa-lab` root-owned0700, а install contract теста требует nonroot owner UID и `setfacl`. Не выполнять массовую смену владельцев боевого checkout/volume. Нужна адаптация через review существующего `Server-setings` и безопасное разделение привилегий; 11 официальных 1Panel функций **не** означают полный root-доступ.
3. **Yandex write + config cloud roundtrip**, затем **полный ночной ASA PostgreSQL+MinIO backup**, затем **изолированный restore**. Нет права объявлять готовыми копии без SHA256, расшифрования владельцем и теста восстановления.
4. **Tailscale SSH.** UFW разрешает **только** D2-R2 `100.96.255.113` → `tailscale0:22`, key-only sshd слушает порт22. При TCP проверке с D2-R2 соединение не проходит и счётчик этого UFW правила остаётся нулевым, хотя ICMP/TSMP идёт. Нужно проверить Tailscale grants/ACL либо внешние клиентские ограничения; не расширять firewall всем узлам.

## Другие пункты перед заявлением о production-ready

- Browser acceptance: вход преподавателя/ученика, курсы, проекты, Scratch/Electronics save/reopen, документы/загрузки.
- Проверка фактического продления TLS на Leticia и регулярный alert о сроках (сам факт действительного сертификата не доказывает autoRenew).
- Тест автостарта после reboot, отказоустойчивости сетевого канала и возможность recovery без D2-R2/тестовой VM.
- Ветка PR #27 пока Draft; CI SUCCESS на последнем проверенном SHA, однако merge в `main`, независимый review и post-merge CI не выполнены. Не смешивать установленную live ветку VM100 с принятым состоянием `main`.
- Для будущей смены публичного домена есть `docs/30-SCHOOL-DOMAIN-MIGRATION-RUNBOOK.md`. **Менять основной `asa-lab.ru` сейчас не требуется и нельзя без отдельного cutover/rollback.**

Принцип: `PASS` только после реального end-to-end приёма соответствующей функции, не после одного Docker Healthy, CI либо OAuth connected-for-read.
