# Диагностика безопасности и план SSH / MCP / 1Panel / FRP

23 октября 2026. Проверен настоящий тестовый узел admins-MS-7D22: SSH с проверкой host key, sudo read-only диагностика, firewall IPv4/IPv6/Docker, эффективный sshd config, metadata контейнеров и файлов, API1Panel, защищённый MCP и его новый update-plan. Конфигурация, firewall, DNS, SSH, службы и рабочая БД не менялись. Preflight сохраняет свой план и может обновлять Git/image cache; это не применение обновления. Production/школьный узел не проверялись и не изменялись.

Это фактическая конфигурационная диагностика. Полная внешняя проверка/пентест, CVE-сканирование образов, проверка восстановления и установка на второй чистой ОС не завершены. Сервер пока не признаётся готовым для production. Снимок без секретов: [security-readiness.json](../snapshots/2026-10-23/security-readiness.json).

## Наблюдаемое состояние и приоритеты

| Проверка | Факт | Действие |
|---|---|---|
| P1: секрет MCP | Реальный Panel API-key содержится в /opt/1panel/docker/compose/asa-1panel-mcp/.env mode0644; test -r от nobody успешен. Значения ключей не выводились | Закрыть файл и его окружение, заменить API-key/затронутые секреты, проверить зависимости и обновить квитанцию. Не считать смену chmod достаточной ротацией |
| SSH | LAN password+sudo работает; admins authorized_keys пуст, passwordauthentication=yes, root key login допускается | Установить отдельный owner Ed25519 key, проверить второй сеанс и восстановление, затем запретить password/root login по выбранному профилю. Не отключать текущий пароль до проверенного key login |
| Второй канал | tailscaled active; попытка SSH из D2-R2 к tailnetIP остановилась локально WinError10013 | Проверить с другого разрешённого устройства; текущая ошибка не доказывает отказ Ubuntu. Windows DNS/VPN/firewall не менять |
| Firewall | UFW active, default deny;22/19192 разрешены LAN /24 и tailnet,80/443 публичны. ts-input принимает весь tailscale0 до UFW | Проверить tailnet ACL, сузить admin sources без потери доступа; учесть IPv6/Docker. Нельзя считать UFW-правила полной tailnet ACL |
| 1Panel | core/agent active; HTTP0.0.0.0:19192, SSL Disable, domain/IP binding пустые, secure entrance есть, session86400s | Для удалённого администрирования HTTPS, MFA/passkey с проверенным восстановлением, ограничения login/session. MFA сейчас UNKNOWN, не доказано отключение |
| БД/объекты | PostgreSQL/MinIO без host port publishing; API без host port; web/Scratch loopback | Сохранить. Каталог БД1Panel не отражает Compose PostgreSQL |
| Изоляция ASA | Running web/API/Scratch/MinIO: nonroot, cap_drop ALL, no-new-privileges, readonly rootfs; без Docker socket mount | Сохранить в переносимом профиле. Postgres имеет writable rootfs; это само по себе не CVE |
| Привилегии оператора | admins в docker и sudo; MCP backend работает от admins | Docker group практически даёт root-возможности. Аутентификация/учёт действий обязательны; нельзя обещать песочницу для взломанного доверенного оператора |
| MCP | Public401 без токена; приватные backend/gateway только loopback; gateway full | Сохранить единый OAuth endpoint, проверять expiry/audience/PKCE/revocation и отсутствие утечек. Полный режим не равен отсутствию аутентификации |
| Backup | В /srv/asa-lab/backups0 файлов; /var/backups/server-settings отсутствует; проверенные timers/crontab и1Panel cron0 не подтвердили регулярный ASA backup | Настроить шифрованный offsite backup БД+MinIO+ключей и периодический restore. Внешние неизвестные snapshots не объявляются отсутствующими |
| Обновления | Cached apt83 upgradable, reboot-required есть; unattended-upgrades active | В окно обслуживания обновить проверенные пакеты, reboot с fallback и health/MCP/SSH acceptance.83 — не число доказанных CVE |
| Уязвимости | GitHub ASA11 alerts:9 medium/2 low, часть development; инфраструктурный Dependabot disabled/403; Trivy/Grype/Lynis не обнаружены | Разобрать runtime/dev применимость и сканировать конкретные deployed images/OS. Нет утверждения, что source alerts равны уязвимостям текущей сборки |
| Защита от ботов | Fail2ban client не установлен; в проверенных vhost нет limit_req/limit_conn | Проверить application throttling и все includes; затем настроить budgets для login/API/MCP. Не отключать штатный BotCheck и не вводить CAPTCHA перед machine MCP |

В SSH-журнале за7дней (максимум10000 строк)1 failed password,1 invalid user; это не подтверждение отсутствия взлома. Историческая утечка диагностических секретов20октября также требует подтверждённой ротации; ротация в рамках анализа не выполнялась.

## Что значит полный MCP и почему жалуются агенты

Реальный tools/list backend отдал19 методов:11 официальных1Panel,6 ASA update/verify/finalize,2 restore plan/rehearse. В подключённом клиенте доступно17: restore plan/rehearse не загружены. На новом узле базовый setup-mcp.sh имеет только readonly/readwrite и не воспроизводит автоматически существующий full OAuth gateway; это отдельный пробел bootstrap.

Свежий реальный asa_update_plan45b4793c9b064255910b34c7c78975b1: ready=true,8 checks PASS, blockers=[], installed6e80e3f3/schema234 → published stableb9a2f7a/schema236. Во время анализа вышел новый stable; он проверен повторным планом. Запуск обновления/миграции не выполнялся. Успешный plan не доказывает успешный switch и полную приёмку новой версии.

| Возможность | Сейчас |
|---|---|
| Данные панели, сайты/SSL/БД, установка MySQL/OpenResty | Официальные11 tools, full |
| Штатное обновление установленной ASA |6 durable tools; preflight готов |
| Первичная установка на пустой узел через MCP | Специальной операции нет; пока canonical CLI |
| Restore plan/rehearse | Есть на сервере, не в кеше клиента |
| Restore apply / чистая переустановка через обычный MCP | Нет штатного метода; отдельный проверенный оператор не означает наличие MCP capability |
| Произвольный Linux shell | В этих19 методах нет. Полный SSH владельца остаётся отдельным административным каналом |

Нужна матрица версий capabilities и bootstrap, а не отключение проверок. Для полноты добавляются типизированные install/restore/apply/recovery/диагностика как durable jobs: фиксированные root/project, backup/rollback, digest/CI/schema checks, progress/timeout и проверка результата. Сохраняется full для доверенных owner-агентов, индивидуальная OAuth-идентификация, отзыв токенов и аудит. Подключение обновляет tools/list; UI-имя full не создаёт отсутствующие операции. Точные прошлые отказы нужно различать: отсутствующий tool,401/403, stale plan, блокировка схемы/backup/receipt, неуспех приёмки или ещё не опубликованный stable.

## Поддомен1panel.asa-lab.ru

Владелец подтвердил именно1panel.asa-lab.ru. Сейчас DNS-over-HTTPS вернул NXDOMAIN; запись не создавалась.

Предпочтительный профиль: HTTPS-панель доступна доверенным устройствам через Tailscale, DNS указывает на tailnet-адрес (или используется split DNS), сертификат выдаётся через DNS-01. Домен остаётся привычным адресом, а порт19192 не публикуется. Необходима проверка реального второго канала и ACL; active tailscaled недостаточно.

Если нужен доступ обычным браузером без VPN: публичный DNS → существующий1Panel/OpenResty:443 → loopback19192. Перед открытием обязательны закрытие/ротация ключей, MFA/passkey, проверенный TLS, login throttling, журнал и recovery. Сам19192 остаётся закрыт с WAN. Настройки proxy/сертификата выполняются в1Panel API/UI; генерируемые vhost/DB вручную не переписываются. Локальный HTTP между proxy и panel остаётся loopback, а browser-leg — HTTPS; binding Host проверяется отдельно.

SSH остаётся полноценным через ключ+sudo по LAN/tailnet. MFA/защитный путь панели не заменяют SSH key authentication. Публичный cloud MCP сохраняет текущий test.asa-lab.ru/mcp с OAuth, без браузерной CAPTCHA/дополнительного interactive-login на каждом tool call. Его нельзя просто спрятать за VPN, недоступным облачному клиенту. Миграция issuer/domain MCP не нужна для создания panel-поддомена.

## Один FRP для нескольких проектов

Да: один frpc на школьном сервере соединяется с одним frps на публичном узле; конфигурация содержит несколько proxies для разных localPort. Это общий транспорт с несколькими каналами, а не один порт приложения для всех сервисов. FRP HTTP/HTTPS также умеет маршрутизацию доменов. Для существующего OpenResty удобнее отдельные loopback TCP endpoints FRP и доменные сайты в1Panel, чтобы FRP не занимал80/443.

Предлагаемые значения ниже — проект, не созданные DNS/порты:

| Внешний адрес | На узле1Panel | Назначение |
|---|---|---|
| test.asa-lab.ru | существующий127.0.0.1:4610 | ASA на самом тестовом узле, без FRP |
| school.asa-lab.ru | FRP127.0.0.1:14610 | школьный frpc → локальная ASA:4610 |
| publisher.asa-lab.ru | FRP127.0.0.1:14620 | школьный frpc → публикатор:4620 |
| iso.asa-lab.ru | FRP127.0.0.1:14630 | школьный frpc → другой HTTP-проект:4630 |
|1panel.asa-lab.ru | отдельный защищённый proxy | управление публичным узлом |

Порты приложений необходимо подтвердить перед применением. Если публикатор/ISO находятся на самом публичном узле, они получают собственные локальные upstream и не нуждаются в обходе через школьный туннель. Контейнеры одного проекта общаются по внутренней Docker network/service DNS; разные проекты получают отдельные сети, credentials, БД и permissions. Браузер обращается по доменам; cookies/Origin/CORS и OAuth redirects настраиваются отдельно, широкие shared cookies для всех поддоменов не нужны.

FRP: зафиксированная проверенная версия+checksum, отдельная service identity, TLS с проверкой server certificate (предпочтительно mTLS), auth, proxyBindAddr=127.0.0.1, allowPorts только согласованные14610/14620/14630, закрытый dashboard и автозапуск/мониторинг. Управляющий порт frps (например7000) отдельно разрешается для frpc; при домашнем NAT требуется подтверждённый port-forward на роутере, при CGNAT — доступный публичный ingress/VPS (production leticia автоматически не используется); широкие диапазоны портов наружу не открываются. SSH/DB/Docker API не добавляются в общий публичный web tunnel.

Один443 обслуживает несколько HTTPS-доменов через OpenResty/SNI/Host. Произвольные не-HTTP TCP-протоколы требуют отдельных external ports либо совместимого tcpmux-клиента; обычный SSH сам не маршрутизируется по HTTP Host. В одном FRP все проекты делят uplink и точку отказа. Надо проверить websocket/SSE, .sb3/image uploads, forwarded IP anti-spoofing, тайм-ауты, reconnect/reboot и независимые resource limits. От объёмного DDoS домашний сервер/FRP сами по себе не защищают: нужна защита на стороне публичного ingress/провайдера.

## Воспроизводимость и порядок внедрения

GitHub уже содержит install.sh/serverctl/panelctl, versions.lock, официальный MCP, OAuth patch/gateway artifacts, ASA operations, portable manager и migration/backup contracts. Это части системы; второй чистый production-узел с полным owner/bot профилем ещё не воспроизводился автоматически.

1. Закрыть P1-readable gateway secret, ротировать затронутые ключи, подтвердить401 без токена и штатные owner tool calls. Не печатать secrets и не считать отсутствие файла по старому пути отсутствием защиты.
2. Установить owner SSH-key и проверить LAN+tailnet+консоль fallback. Только после этого менять SSH policy. Не менять Windows network настройки и не выключать пароль вслепую.
3. Подготовить HTTPS/MFA панели и подтверждённый admin DNS. Сохранить recovery и прямой закрытый19192. Настроить edge/application budgets так, чтобы легитимный MCP и classroom users проходили.
4. Зафиксировать обновлённые OS/1Panel/Docker/FRP версии и контрольные суммы; выполнить package/image vulnerability review, rotation manifest и post-reboot acceptance. Никакой подмены OpenResty параллельным публичным Caddy.
5. Добавить автоматическое шифрованное offsite резервирование БД+MinIO+config/ключей, уведомления и тест restore. Предлагаемый минимум: ежедневный backup и ежемесячный restore drill; RPO/RTO утвердить до боевой нагрузки.
6. Довести bootstrap-профиль trusted-owner/full-MCP: одна manifest-driven установка, приватные0600 secrets/0700 dirs, OAuth account enrollment, full capability inventory, TLS, schedule и служебные аккаунты. В репозитории только templates/public values; private keys/passwords/tokens не коммитятся.
7. Добавить недостающие install/restore operations без слепого shell из публичного MCP. Для уже работающей ASA использовать штатный update; для восстановления — отдельные plan/rehearse/apply с явной data policy.
8. Подключить FRP и отдельные project routes в1Panel; протестировать каждый проект и общий restart/reconnect. Wildcard app allowlist/DNS/certificate — разные механизмы: допустимый поддомен сам не создаёт DNS, vhost или права. Неизвестный Host не должен попадать в админку.
9. Проверить весь профиль на отдельной чистой Ubuntu: reboot, owner SSH/MFA recovery, public negative access, OAuth revocation, full19+ inventory, install/update/rollback, archive restore, Scratch/electronics, multi-project FRP и backups. Сохранить exact SHA/версии/квитанцию. Только после этого признавать профиль production-ready.

Критерии приёмки: посторонний не читает secrets и не вызывает MCP; владелец сохраняет SSH+панель; доверенный агент выполняет полный поддержанный lifecycle; DB/object/Docker/control ports закрыты; backup действительно восстанавливается; каждый проект работает под своим доменом после reboot. Абсолютная гарантия отсутствия взлома не заявляется.

Официальные основания: [1Panel security settings](https://1panel.pro/docs/v2/user_manual/settings/), [FRP server options](https://gofrp.org/en/docs/reference/server-configures/), [FRP HTTP/HTTPS](https://gofrp.org/en/docs/features/http-https/), [FRP authentication](https://gofrp.org/en/docs/features/common/authentication/), [FRP TLS certificate verification](https://gofrp.org/en/docs/features/common/network/network-tls/).
